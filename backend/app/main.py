"""
InfraTrace read-only API.

Architectural rule, enforced throughout: the DATABASE is the source of truth.
Every route is a thin wrapper that binds parameters, calls SQL, and shapes the
JSON. Dependency traversal, blast radius, risk scoring and cycle detection are
all computed by recursive CTEs, stored functions and views inside MySQL. None
of that logic is reimplemented here - if it were, there would be two
definitions of "blast radius" free to disagree with each other.

The API is deliberately READ-ONLY. InfraTrace's value in this phase is
analysis, and a read-only surface cannot corrupt the dataset a reviewer is
about to inspect. Write endpoints are listed in the future scope.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import queries as q
from .db import ConfigError, call_proc, ping, query_all, query_one

load_dotenv()

app = FastAPI(
    title="InfraTrace API",
    description=(
        "Service dependency and impact intelligence, served directly from a "
        "normalised MySQL schema. All analysis is computed in SQL."
    ),
    version="1.0.0",
)

# CORS: the dashboard is served from a different origin during development.
# The allowlist comes from the environment so production is not forced to
# accept a wildcard. Defaults cover a local static server only.
_origins = os.getenv(
    "CORS_ALLOW_ORIGINS",
    "http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080,http://127.0.0.1:8080",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------
@app.exception_handler(ConfigError)
async def config_error_handler(_request, exc: ConfigError) -> JSONResponse:
    """Missing configuration is an operator problem, so say so clearly."""
    return JSONResponse(
        status_code=503,
        content={"error": "configuration_error", "detail": str(exc)},
    )


def _require_component(component_id: int) -> dict:
    """Fetch a component or raise 404. Used by every /components/{id}/... route."""
    row = query_one(q.COMPONENT_BY_ID, (component_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"No component with id {component_id}")
    return row


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------
@app.get("/health", tags=["meta"])
def health() -> dict[str, Any]:
    """
    Liveness plus a real schema check.

    Returns the MySQL version and object counts, so a 200 here means the
    database is reachable AND the schema is actually loaded - not merely
    that the web process is running.
    """
    try:
        info = ping()
    except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
        raise HTTPException(
            status_code=503, detail=f"Database not reachable: {exc}"
        ) from exc
    return {"status": "ok", "database": info}


# ---------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------
@app.get("/components", tags=["components"])
def list_components(
    component_type: Annotated[str | None, Query(max_length=20)] = None,
    criticality: Annotated[str | None, Query(max_length=10)] = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """
    Every component with its risk metrics, newest-risk-first.

    The optional filters are applied in SQL with the `%s IS NULL OR col = %s`
    idiom, so one prepared statement serves every filter combination and the
    values are always bound, never interpolated.
    """
    active = None if is_active is None else int(is_active)
    rows = query_all(
        q.COMPONENTS_LIST,
        (
            component_type, component_type,
            criticality, criticality,
            active, active,
            limit, offset,
        ),
    )
    return {"count": len(rows), "components": rows}


@app.get("/components/{component_id}", tags=["components"])
def get_component(component_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """One component, with the metrics computed by the stored functions."""
    return _require_component(component_id)


@app.get("/components/{component_id}/dependencies", tags=["components"])
def get_dependencies(
    component_id: Annotated[int, Path(ge=1)],
    recursive: bool = False,
) -> dict[str, Any]:
    """
    What this component depends on.

    recursive=false - direct dependencies only.
    recursive=true  - the full transitive chain, via sp_get_dependencies,
                      with the depth at which each one is reached.
    """
    component = _require_component(component_id)
    if recursive:
        result_sets = call_proc("sp_get_dependencies", (component_id,))
        rows = result_sets[0] if result_sets else []
    else:
        rows = query_all(q.COMPONENT_DEPENDENCIES, (component_id,))
    return {
        "component_id": component_id,
        "component_name": component["component_name"],
        "recursive": recursive,
        "count": len(rows),
        "dependencies": rows,
    }


@app.get("/components/{component_id}/dependents", tags=["components"])
def get_dependents(component_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """What depends directly on this component - who to notify before a change."""
    component = _require_component(component_id)
    rows = query_all(q.COMPONENT_DEPENDENTS, (component_id,))
    return {
        "component_id": component_id,
        "component_name": component["component_name"],
        "count": len(rows),
        "dependents": rows,
    }


@app.get("/components/{component_id}/blast-radius", tags=["components"])
def get_blast_radius(component_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """
    Full potential impact if this component fails.

    Computed by sp_get_blast_radius (a recursive CTE), plus the affected
    applications, the teams to page, and the propagation paths.

    IMPORTANT - what this is: dependency-based POTENTIAL impact. It does NOT
    model redundancy, failover, graceful degradation, circuit breakers or
    live health, because none of those are recorded in the schema. Read it as
    "these components have a dependency path to the failure", not "these
    components will go down". The caveat is returned in the payload so a
    consumer cannot lose it.
    """
    component = _require_component(component_id)

    proc = call_proc("sp_get_blast_radius", (component_id,))
    affected = proc[0] if proc else []

    return {
        "component_id": component_id,
        "component_name": component["component_name"],
        "blast_radius": component["blast_radius"],
        "risk_score": component["risk_score"],
        "affected_components": affected,
        "affected_applications": query_all(q.COMPONENT_AFFECTED_APPLICATIONS, (component_id,)),
        "affected_teams": query_all(q.COMPONENT_AFFECTED_TEAMS, (component_id,)),
        "propagation_paths": query_all(q.COMPONENT_BLAST_PATHS, (component_id,)),
        "subgraph_edges": query_all(q.COMPONENT_BLAST_EDGES, (component_id,)),
        "caveat": (
            "Dependency-based potential impact. Does not model redundancy, "
            "failover, graceful degradation, circuit breakers or live health."
        ),
    }


@app.get("/components/{component_id}/incidents", tags=["components"])
def get_component_incidents(component_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """This component's incident history, most recent first."""
    component = _require_component(component_id)
    rows = query_all(q.COMPONENT_INCIDENTS, (component_id,))
    return {
        "component_id": component_id,
        "component_name": component["component_name"],
        "count": len(rows),
        "incidents": rows,
    }


@app.get("/components/{component_id}/deployments", tags=["components"])
def get_component_deployments(component_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """This component's deployment history across all environments."""
    component = _require_component(component_id)
    rows = query_all(q.COMPONENT_DEPLOYMENTS, (component_id,))
    return {
        "component_id": component_id,
        "component_name": component["component_name"],
        "count": len(rows),
        "deployments": rows,
    }


@app.get("/components/{component_id}/impact-report", tags=["components"])
def get_impact_report(component_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """
    The complete picture for one component in a single database round trip.

    sp_component_impact_report returns FIVE result sets, which is precisely
    why db.call_proc walks nextset() instead of calling fetchall() once.
    """
    _require_component(component_id)
    sets = call_proc("sp_component_impact_report", (component_id,))
    names = [
        "component",
        "depends_on",
        "blast_radius",
        "affected_applications",
        "incident_history",
    ]
    return {
        "component_id": component_id,
        "sections": {name: (sets[i] if i < len(sets) else [])
                     for i, name in enumerate(names)},
    }


# ---------------------------------------------------------------------
# Teams, applications, environments
# ---------------------------------------------------------------------
@app.get("/teams", tags=["organisation"])
def list_teams() -> dict[str, Any]:
    """Teams with headcount, what they own, and their share of platform risk."""
    rows = query_all(q.TEAMS_LIST)
    return {"count": len(rows), "teams": rows}


@app.get("/teams/{team_id}", tags=["organisation"])
def get_team(team_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """One team, its engineers, and a per-component health report."""
    team = query_one(q.TEAM_BY_ID, (team_id,))
    if team is None:
        raise HTTPException(status_code=404, detail=f"No team with id {team_id}")
    proc = call_proc("sp_team_health_report", (team_id,))
    return {
        "team": team,
        "developers": query_all(q.TEAM_DEVELOPERS, (team_id,)),
        "component_health": proc[0] if proc else [],
    }


@app.get("/applications", tags=["organisation"])
def list_applications() -> dict[str, Any]:
    """Applications and how much infrastructure each one depends on."""
    rows = query_all(q.APPLICATIONS_LIST)
    return {"count": len(rows), "applications": rows}


@app.get("/applications/{application_id}/components", tags=["organisation"])
def get_application_components(
    application_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    """The components one application uses, riskiest first."""
    rows = query_all(q.APPLICATION_COMPONENTS, (application_id,))
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No application with id {application_id}, or it uses no components",
        )
    return {"application_id": application_id, "count": len(rows), "components": rows}


@app.get("/environments", tags=["organisation"])
def list_environments() -> dict[str, Any]:
    """Environments and how much has been deployed into each."""
    rows = query_all(q.ENVIRONMENTS_LIST)
    return {"count": len(rows), "environments": rows}


# ---------------------------------------------------------------------
# Incidents and deployments
# ---------------------------------------------------------------------
@app.get("/incidents", tags=["operations"])
def list_incidents(
    severity: Annotated[str | None, Query(max_length=10)] = None,
    status: Annotated[str | None, Query(max_length=15)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Incidents with their affected components, most recent first."""
    rows = query_all(
        q.INCIDENTS_LIST,
        (severity, severity, status, status, limit, offset),
    )
    return {"count": len(rows), "incidents": rows}


@app.get("/incidents/{incident_id}", tags=["operations"])
def get_incident(incident_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """One incident and every component it affected, root cause first."""
    incident = query_one(q.INCIDENT_BY_ID, (incident_id,))
    if incident is None:
        raise HTTPException(status_code=404, detail=f"No incident with id {incident_id}")
    return {
        "incident": incident,
        "affected_components": query_all(q.INCIDENT_COMPONENTS, (incident_id,)),
    }


@app.get("/deployments", tags=["operations"])
def list_deployments(
    environment: Annotated[str | None, Query(max_length=30)] = None,
    status: Annotated[str | None, Query(max_length=15)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Deployment history, most recent first."""
    rows = query_all(
        q.DEPLOYMENTS_LIST,
        (environment, environment, status, status, limit, offset),
    )
    return {"count": len(rows), "deployments": rows}


# ---------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------
@app.get("/analytics/summary", tags=["analytics"])
def analytics_summary() -> dict[str, Any]:
    """Every dashboard headline number, from one query of scalar subqueries."""
    return query_one(q.ANALYTICS_SUMMARY) or {}


@app.get("/analytics/high-risk-components", tags=["analytics"])
def analytics_high_risk(
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict[str, Any]:
    """
    Components ranked by risk score (blast radius x criticality weight).

    The score comes from fn_risk_score, so the ranking the dashboard shows is
    identical to the one the SQL reports produce.
    """
    rows = query_all(q.ANALYTICS_HIGH_RISK, (limit,))
    return {
        "count": len(rows),
        "components": rows,
        "scoring": "fn_risk_score = blast_radius x criticality weight (Critical 4, High 3, Medium 2, Low 1)",
    }


@app.get("/analytics/critical-incidents", tags=["analytics"])
def analytics_critical_incidents(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """SEV1 and SEV2 incidents with the component and responsible team."""
    rows = query_all(q.ANALYTICS_CRITICAL_INCIDENTS, (limit,))
    return {"count": len(rows), "incidents": rows}


@app.get("/analytics/team-health", tags=["analytics"])
def analytics_team_health() -> dict[str, Any]:
    """Risk aggregated per owning team. The UNASSIGNED row is the one to read."""
    rows = query_all(q.ANALYTICS_TEAM_HEALTH)
    return {"count": len(rows), "teams": rows}


@app.get("/analytics/production-infrastructure", tags=["analytics"])
def analytics_production() -> dict[str, Any]:
    """
    What is live in production, at which version.

    Straight from production_infrastructure_view, which resolves the latest
    SUCCESSFUL production deployment per component and therefore reports the
    rolled-back-to version rather than the version that was rolled back.
    """
    rows = query_all(q.ANALYTICS_PRODUCTION)
    return {"count": len(rows), "components": rows}


@app.get("/analytics/unowned-components", tags=["analytics"])
def analytics_unowned() -> dict[str, Any]:
    """Components with no owning team - nobody would be paged if they failed."""
    rows = query_all(q.ANALYTICS_UNOWNED)
    return {"count": len(rows), "components": rows}


@app.get("/analytics/incident-trend", tags=["analytics"])
def analytics_incident_trend() -> dict[str, Any]:
    """
    Monthly incident counts with cumulative total, month-on-month change and
    a three-month moving average - all computed by SQL window functions.
    """
    rows = query_all(q.ANALYTICS_INCIDENT_TREND)
    return {"count": len(rows), "trend": rows}


@app.get("/analytics/deploy-incident-correlation", tags=["analytics"])
def analytics_deploy_incident_correlation() -> dict[str, Any]:
    """
    Production deployments followed within 7 days by an incident on the same
    component.

    causal_status separates the two ideas deliberately: CONFIRMED means a
    post-incident review recorded this deployment in
    incident.caused_by_deployment_id. CORRELATION ONLY means the timing lines
    up and nothing more. Collapsing them would overstate what the data shows.
    """
    rows = query_all(q.ANALYTICS_DEPLOY_INCIDENT_CORRELATION)
    confirmed = sum(1 for r in rows if r["causal_status"] == "CONFIRMED")
    return {
        "count": len(rows),
        "confirmed_causes": confirmed,
        "correlations_only": len(rows) - confirmed,
        "results": rows,
    }


# ---------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------
@app.get("/graph", tags=["graph"])
def get_graph() -> dict[str, Any]:
    """
    The whole dependency graph as nodes and edges, for visualisation.

    An edge runs source -> target meaning "source DEPENDS ON target". The
    relational database remains authoritative; this is only a projection of
    it into a shape a drawing library can consume.
    """
    return {
        "nodes": query_all(q.GRAPH_NODES),
        "edges": query_all(q.GRAPH_EDGES),
        "edge_direction": "source depends on target",
    }


@app.get("/graph/cycles", tags=["graph"])
def get_cycles() -> dict[str, Any]:
    """
    Any circular dependency in the graph.

    Triggers PREVENT cycles on every INSERT and UPDATE, so on a healthy
    database this is empty. It still matters, because triggers can be
    bypassed by a bulk load or a restored dump - this is the audit that
    proves the graph is genuinely acyclic.
    """
    rows = query_all(q.CYCLE_CHECK)
    return {
        "acyclic": len(rows) == 0,
        "cycles_found": len(rows),
        "cycles": rows,
    }
