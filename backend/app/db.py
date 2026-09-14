"""
Database access for the InfraTrace API.

Design rule for this whole package: the DATABASE is the source of truth.
This module opens connections and runs SQL. It does not contain analysis
logic. Blast radius, risk scores and dependency traversal are computed by
the stored functions, procedures and views in database/ - never
reimplemented in Python, because then there would be two definitions of
"blast radius" that could disagree.

Security:
  * Credentials come from environment variables only. Nothing is hardcoded
    and no default password is supplied.
  * Every query is parameterised with %s placeholders, so user input is
    always sent as data and can never be parsed as SQL.
  * The API user only needs SELECT and EXECUTE. See .env.example.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv

load_dotenv()


def _normalise(value: Any) -> Any:
    """
    Convert MySQL DECIMAL values into plain JSON numbers.

    MySQL returns SUM(), AVG() and window-function results as DECIMAL, which
    PyMySQL maps to Python's Decimal. Decimal is not JSON-serialisable, so
    FastAPI falls back to rendering it as a STRING - a client then receives
    "22" instead of 22 and has to guess which numeric fields are quoted.

    Converting here, once, keeps every endpoint's JSON types consistent:
    integral values become int, fractional ones become float.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _normalise_row(row: dict) -> dict:
    return {k: _normalise(v) for k, v in row.items()}


def _normalise_rows(rows: Any) -> list[dict]:
    return [_normalise_row(r) for r in rows]


class ConfigError(RuntimeError):
    """Raised when required database configuration is missing."""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"Environment variable {name} is not set. "
            f"Copy backend/.env.example to backend/.env and fill it in."
        )
    return value


def _connection_settings() -> dict[str, Any]:
    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": _require("DB_USER"),
        "password": _require("DB_PASSWORD"),
        "database": os.getenv("DB_NAME", "infratrace"),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        # Read-only API: never leave a transaction open holding row locks.
        "autocommit": True,
    }


@contextmanager
def get_cursor() -> Iterator[DictCursor]:
    """Yield a dict cursor, always closing the connection afterwards."""
    conn = pymysql.connect(**_connection_settings())
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def query_all(sql: str, params: tuple | None = None) -> list[dict]:
    """Run a SELECT and return every row, with DECIMALs normalised."""
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return _normalise_rows(cur.fetchall())


def query_one(sql: str, params: tuple | None = None) -> dict | None:
    """Run a SELECT and return the first row, or None."""
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return _normalise_row(row) if row else None


def call_proc(proc_name: str, args: tuple = ()) -> list[list[dict]]:
    """
    CALL a stored procedure and return ALL of its result sets.

    sp_component_impact_report returns five result sets, so a single
    fetchall() would silently discard four of them.

    proc_name is validated against an allowlist rather than interpolated
    blindly: a procedure name cannot be a bound parameter in MySQL, so the
    only safe way to build this statement is to refuse any name that is not
    known in advance.
    """
    allowed = {
        "sp_get_dependencies",
        "sp_get_blast_radius",
        "sp_team_health_report",
        "sp_component_impact_report",
        "sp_detect_dependency_cycles",
    }
    if proc_name not in allowed:
        raise ValueError(f"Unknown procedure: {proc_name}")

    placeholders = ", ".join(["%s"] * len(args))
    sql = f"CALL {proc_name}({placeholders})"

    conn = pymysql.connect(**_connection_settings())
    try:
        results: list[list[dict]] = []
        with conn.cursor() as cur:
            cur.execute(sql, args)
            while True:
                rows = cur.fetchall()
                # EVERY result set is appended, including empty ones.
                #
                # Skipping empties would corrupt the positional mapping that
                # callers rely on: sp_component_impact_report returns five
                # sections in a fixed order, and Payment DB legitimately has
                # no dependencies, so section 2 is empty. Dropping it shifted
                # every later section up by one and the caller read the
                # affected-applications rows as the blast radius.
                results.append(_normalise_rows(rows) if rows else [])
                if not cur.nextset():
                    break

        # MySQL appends a final empty set for the procedure's own OK packet.
        # Trim ONE trailing empty set so callers see only real sections.
        if len(results) > 1 and results[-1] == []:
            results.pop()
        return results
    finally:
        conn.close()


def ping() -> dict:
    """Health check: confirm the database answers and the schema is present."""
    with get_cursor() as cur:
        cur.execute("SELECT VERSION() AS version, DATABASE() AS db")
        info = _normalise_row(cur.fetchone() or {})
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM information_schema.tables
                  WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE') AS tables,
                (SELECT COUNT(*) FROM information_schema.views
                  WHERE table_schema = DATABASE())                               AS views,
                (SELECT COUNT(*) FROM information_schema.routines
                  WHERE routine_schema = DATABASE())                             AS routines,
                (SELECT COUNT(*) FROM component)                                 AS components
            """
        )
        counts = _normalise_row(cur.fetchone() or {})
    return {**info, **counts}
