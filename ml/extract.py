"""
Pull the real InfraTrace graph and its recorded incidents out of MySQL.

THE DATABASE STAYS THE SOURCE OF TRUTH
--------------------------------------
backend/app/db.py sets the rule for this project: blast radius and
dependency traversal are defined by the SQL, never reimplemented in Python,
because two definitions eventually disagree. ml/graph.py nonetheless
contains a `reachable_from` traversal, which it needs in order to run on
synthetic graphs that are not in any database.

That is a second implementation, so `verify_against_sql` exists to prove the
two agree: it compares the Python traversal against `fn_blast_radius_count`
for every component and raises on any mismatch. It is called by the real-data
evaluation and by the test suite. If someone changes the recursive CTE, this
fails loudly instead of the model quietly scoring against a stale notion of
reachability.

CREDENTIALS
-----------
Read from the same environment variables backend/app/db.py uses, via
backend/.env. Nothing is hardcoded and no password is printed. A read-only
account is sufficient - this module only issues SELECTs and one function
call.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .graph import Cascade, Graph, from_edges

REPO_ROOT = Path(__file__).resolve().parent.parent


def _connect():
    """Open a MySQL connection using the backend's configuration."""
    import pymysql
    from dotenv import load_dotenv
    from pymysql.cursors import DictCursor

    load_dotenv(REPO_ROOT / "backend" / ".env")
    load_dotenv()

    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", "3306"))
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    database = os.getenv("DB_NAME", "infratrace")

    if not user or password is None:
        raise RuntimeError(
            "Database credentials are missing. ml/extract.py reads the same "
            "variables as the API: DB_USER and DB_PASSWORD, from "
            "backend/.env. See backend/.env.example."
        )

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        cursorclass=DictCursor,
        charset="utf8mb4",
        # Matches the collation the schema is built with. Without this the
        # client's platform-derived default (cp850 on this Windows box,
        # latin1 elsewhere) can collide with the database collation and
        # raise "Illegal mix of collations" on joins against string columns.
        init_command="SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci",
    )


def load_graph(conn=None) -> Graph:
    """Build a Graph from the `component` and `dependency` tables."""
    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT component_id, component_name, component_type, "
                "criticality, is_active FROM component ORDER BY component_id"
            )
            components = list(cur.fetchall())
            cur.execute(
                "SELECT component_id, depends_on_id, dependency_type, is_critical "
                "FROM dependency ORDER BY dependency_id"
            )
            dependencies = list(cur.fetchall())
    finally:
        if own:
            conn.close()
    return from_edges(components, dependencies)


def load_incident_cascades(graph: Graph, conn=None) -> list[Cascade]:
    """
    Turn recorded incidents into supervised scenarios.

    An incident qualifies when exactly one of its components is marked
    `RootCause`. That component becomes the scenario's root; every other
    component attached to the incident is a positive label.

    LEAKAGE
    -------
    The label comes from `incident_component`, a table the feature pipeline
    never reads. Node features come from `component` (type, criticality,
    activity, degree) and edge features from `dependency`. No feature is
    derived from incident history, so a component's past incident count
    cannot leak the answer. This matters because `fn_incident_count` and
    `fn_risk_score` do use incident history - and including either as a
    feature would let the model see labels through the back door.
    """
    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT incident_id, component_id, impact_level "
                "FROM incident_component ORDER BY incident_id, component_id"
            )
            rows = list(cur.fetchall())
    finally:
        if own:
            conn.close()

    by_incident: dict[int, list[dict]] = {}
    for r in rows:
        by_incident.setdefault(int(r["incident_id"]), []).append(r)

    cascades: list[Cascade] = []
    for _incident_id, members in sorted(by_incident.items()):
        roots = [m for m in members if m["impact_level"] == "RootCause"]
        if len(roots) != 1:
            # Zero roots means the postmortem never identified one; more than
            # one means the incident had several independent causes. Neither
            # is a single-root propagation scenario, so both are skipped
            # rather than guessed at.
            continue
        try:
            root_index = graph.index_of(int(roots[0]["component_id"]))
        except KeyError:
            continue

        labels = np.zeros(graph.num_nodes, dtype=np.float32)
        labels[root_index] = 1.0
        for m in members:
            if m["impact_level"] == "RootCause":
                continue
            try:
                labels[graph.index_of(int(m["component_id"]))] = 1.0
            except KeyError:
                continue
        cascades.append(Cascade(root_index=root_index, labels=labels))

    return cascades


def verify_against_sql(graph: Graph, conn=None) -> dict:
    """
    Check the Python traversal against the stored function, component by
    component.

    Returns a dict with the comparison; raises ValueError on any mismatch so
    a divergence cannot pass silently.
    """
    own = conn is None
    conn = conn or _connect()
    mismatches: list[dict] = []
    try:
        with conn.cursor() as cur:
            for i, cid in enumerate(graph.node_ids):
                cur.execute("SELECT fn_blast_radius_count(%s) AS n", (int(cid),))
                sql_n = int(cur.fetchone()["n"])
                py_n = int(graph.reachable_from(i).sum())
                if sql_n != py_n:
                    mismatches.append(
                        {
                            "component_id": int(cid),
                            "component_name": graph.names[i],
                            "sql": sql_n,
                            "python": py_n,
                        }
                    )
    finally:
        if own:
            conn.close()

    if mismatches:
        raise ValueError(
            "Python reachability disagrees with fn_blast_radius_count for "
            f"{len(mismatches)} component(s): {mismatches}. The SQL is the "
            "source of truth - fix ml/graph.py, not the database."
        )
    return {"checked": graph.num_nodes, "mismatches": 0}
