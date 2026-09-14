"""
API tests for InfraTrace.

These are INTEGRATION tests, deliberately. The whole point of the API is that
it returns what the database computes, so mocking the database away would test
nothing worth testing. Each test therefore runs against a loaded infratrace
schema and asserts on real values from the ShopSphere dataset.

If the database is not reachable or not loaded, every test SKIPS with a clear
message rather than failing - an unconfigured machine is not a broken API.

Run:
    cd backend
    python -m pytest tests/ -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import ping
from app.main import app


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------
@pytest.fixture(scope="session")
def db_available() -> bool:
    """Skip the whole suite if the database is not reachable or not loaded."""
    try:
        info = ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Database not reachable - configure backend/.env first ({exc})")
    if info.get("tables", 0) < 10:
        pytest.skip("Database reachable but schema not loaded - run database/setup.sql")
    if info.get("components", 0) < 19:
        pytest.skip("Schema present but seed data missing - run database/data/seed.sql")
    return True


@pytest.fixture(scope="session")
def client(db_available: bool) -> TestClient:
    return TestClient(app)


# Known ids from the ShopSphere seed dataset.
PAYMENT_DB = 11
KAFKA = 16
ORDER_SERVICE = 3
API_GATEWAY = 1
PAYMENTS_TEAM = 2


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------
def test_health_reports_a_loaded_schema(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    db = body["database"]
    assert db["tables"] == 10
    assert db["views"] == 4
    assert db["routines"] == 11          # 6 functions + 5 procedures
    assert db["version"].startswith("8.")


# ---------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------
def test_list_components_returns_the_whole_estate(client: TestClient) -> None:
    r = client.get("/components", params={"limit": 500})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 19
    names = {c["component_name"] for c in body["components"]}
    assert "Payment DB" in names
    assert "Kafka Event Bus" in names


def test_components_are_ordered_by_risk_score_descending(client: TestClient) -> None:
    r = client.get("/components", params={"limit": 500})
    scores = [c["risk_score"] for c in r.json()["components"]]
    assert scores == sorted(scores, reverse=True)


def test_component_type_filter_is_applied_in_sql(client: TestClient) -> None:
    r = client.get("/components", params={"component_type": "Database", "limit": 500})
    assert r.status_code == 200
    rows = r.json()["components"]
    assert len(rows) == 4
    assert all(c["component_type"] == "Database" for c in rows)


def test_is_active_filter_excludes_the_retired_component(client: TestClient) -> None:
    active = client.get("/components", params={"is_active": True, "limit": 500}).json()
    retired = client.get("/components", params={"is_active": False, "limit": 500}).json()
    assert active["count"] == 18
    assert retired["count"] == 1
    assert retired["components"][0]["component_name"] == "Legacy Coupon Service"


def test_get_one_component_carries_the_stored_function_metrics(client: TestClient) -> None:
    r = client.get(f"/components/{PAYMENT_DB}")
    assert r.status_code == 200
    c = r.json()
    assert c["component_name"] == "Payment DB"
    assert c["criticality"] == "Critical"
    assert c["owning_team"] == "Payments Team"
    assert c["blast_radius"] == 3           # Payment Svc -> Order Svc -> API GW
    assert c["dependency_depth"] == 0       # depends on nothing
    assert c["risk_score"] == 12            # 3 x Critical weight 4


def test_missing_component_is_404(client: TestClient) -> None:
    assert client.get("/components/99999").status_code == 404


def test_invalid_component_id_is_422(client: TestClient) -> None:
    assert client.get("/components/0").status_code == 422
    assert client.get("/components/abc").status_code == 422
    assert client.get("/components", params={"limit": 9999}).status_code == 422


# ---------------------------------------------------------------------
# Dependencies and dependents
# ---------------------------------------------------------------------
def test_order_service_direct_dependencies(client: TestClient) -> None:
    r = client.get(f"/components/{ORDER_SERVICE}/dependencies")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 5
    names = {d["component_name"] for d in body["dependencies"]}
    assert names == {
        "Payment Service", "Inventory Service", "Orders DB",
        "Kafka Event Bus", "Shipping Service",
    }


def test_recursive_dependencies_reach_deeper_than_direct_ones(client: TestClient) -> None:
    direct = client.get(f"/components/{ORDER_SERVICE}/dependencies").json()
    deep = client.get(
        f"/components/{ORDER_SERVICE}/dependencies", params={"recursive": True}
    ).json()
    assert deep["recursive"] is True
    assert deep["count"] > direct["count"]
    # the transitive chain must include things Order Service does not call directly
    deep_names = {d["depends_on"] for d in deep["dependencies"]}
    assert "Payment DB" in deep_names          # via Payment Service
    assert "Stripe Payment API" in deep_names  # via Payment Service


def test_payment_db_depends_on_nothing(client: TestClient) -> None:
    body = client.get(f"/components/{PAYMENT_DB}/dependencies").json()
    assert body["count"] == 0


def test_kafka_has_six_direct_dependents(client: TestClient) -> None:
    body = client.get(f"/components/{KAFKA}/dependents").json()
    assert body["count"] == 6


def test_api_gateway_has_no_dependents(client: TestClient) -> None:
    """The gateway is the entry point: nothing inside the system depends on it."""
    assert client.get(f"/components/{API_GATEWAY}/dependents").json()["count"] == 0


# ---------------------------------------------------------------------
# Blast radius - the headline feature
# ---------------------------------------------------------------------
def test_payment_db_blast_radius_is_the_expected_chain(client: TestClient) -> None:
    r = client.get(f"/components/{PAYMENT_DB}/blast-radius")
    assert r.status_code == 200
    body = r.json()
    assert body["blast_radius"] == 3

    chain = [(c["hops_away"], c["affected_component"]) for c in body["affected_components"]]
    assert chain == [
        (1, "Payment Service"),
        (2, "Order Service"),
        (3, "API Gateway"),
    ]


def test_blast_radius_reports_affected_applications_and_teams(client: TestClient) -> None:
    body = client.get(f"/components/{PAYMENT_DB}/blast-radius").json()
    apps = {a["application_name"] for a in body["affected_applications"]}
    assert apps == {
        "ShopSphere Web", "ShopSphere Mobile",
        "ShopSphere Admin", "ShopSphere Partner API",
    }
    teams = {t["team_name"] for t in body["affected_teams"]}
    assert {"Payments Team", "Commerce Team", "Platform Engineering"} <= teams


def test_blast_radius_includes_the_propagation_path(client: TestClient) -> None:
    body = client.get(f"/components/{PAYMENT_DB}/blast-radius").json()
    longest = max(body["propagation_paths"], key=lambda p: p["hops"])
    assert longest["hops"] == 3
    assert longest["propagation_path"] == (
        "Payment DB -> Payment Service -> Order Service -> API Gateway"
    )


def test_blast_radius_states_its_own_limitations(client: TestClient) -> None:
    """
    The caveat must travel with the data. A consumer that sees the numbers
    but not the limitation would overstate what the analysis proves.
    """
    caveat = client.get(f"/components/{PAYMENT_DB}/blast-radius").json()["caveat"]
    for word in ("redundancy", "failover", "degradation"):
        assert word in caveat.lower()


def test_kafka_has_the_widest_blast_radius(client: TestClient) -> None:
    body = client.get(f"/components/{KAFKA}/blast-radius").json()
    assert body["blast_radius"] == 7
    assert body["risk_score"] == 28


def test_impact_report_returns_all_five_sections(client: TestClient) -> None:
    """sp_component_impact_report returns five result sets; none may be lost."""
    body = client.get(f"/components/{PAYMENT_DB}/impact-report").json()
    sections = body["sections"]
    assert set(sections) == {
        "component", "depends_on", "blast_radius",
        "affected_applications", "incident_history",
    }
    assert sections["component"][0]["component_name"] == "Payment DB"
    assert len(sections["blast_radius"]) == 3
    assert len(sections["affected_applications"]) == 4
    assert len(sections["incident_history"]) >= 2


# ---------------------------------------------------------------------
# Incidents and deployments
# ---------------------------------------------------------------------
def test_component_incident_history_is_newest_first(client: TestClient) -> None:
    body = client.get(f"/components/{PAYMENT_DB}/incidents").json()
    assert body["count"] >= 2
    starts = [i["started_at"] for i in body["incidents"]]
    assert starts == sorted(starts, reverse=True)


def test_incident_severity_filter(client: TestClient) -> None:
    body = client.get("/incidents", params={"severity": "SEV1", "limit": 500}).json()
    assert body["count"] == 6
    assert all(i["severity"] == "SEV1" for i in body["incidents"])


def test_incident_status_filter_finds_the_open_ones(client: TestClient) -> None:
    total_open = 0
    for status in ("Open", "Investigating", "Mitigated"):
        total_open += client.get(
            "/incidents", params={"status": status, "limit": 500}
        ).json()["count"]
    assert total_open == 3


def test_one_incident_lists_its_components_root_cause_first(client: TestClient) -> None:
    body = client.get("/incidents/1").json()
    assert body["incident"]["severity"] == "SEV1"
    comps = body["affected_components"]
    assert comps[0]["impact_level"] == "RootCause"
    assert comps[0]["component_name"] == "Payment DB"


def test_deployments_are_newest_first(client: TestClient) -> None:
    body = client.get("/deployments", params={"limit": 500}).json()
    assert body["count"] == 46
    times = [d["deployed_at"] for d in body["deployments"]]
    assert times == sorted(times, reverse=True)


def test_deployment_environment_filter(client: TestClient) -> None:
    body = client.get(
        "/deployments", params={"environment": "Production", "limit": 500}
    ).json()
    assert body["count"] > 0
    assert all(d["environment_name"] == "Production" for d in body["deployments"])


# ---------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------
def test_teams_list_has_no_fan_out_inflation(client: TestClient) -> None:
    """
    A team's component count must be the real count. Joining developer,
    component and application together would multiply rows and inflate it -
    the bug this query was rewritten to avoid.
    """
    body = client.get("/teams").json()
    assert body["count"] == 6
    owned = {t["team_name"]: t["components_owned"] for t in body["teams"]}
    assert sum(owned.values()) == 17       # 19 components, 2 unowned
    assert owned["Mobile Team"] == 0       # owns none, must still be listed


def test_team_detail_includes_developers_and_health(client: TestClient) -> None:
    body = client.get(f"/teams/{PAYMENTS_TEAM}").json()
    assert body["team"]["team_name"] == "Payments Team"
    assert len(body["developers"]) == 2
    health = body["component_health"]
    assert len(health) == 3
    assert {h["component_name"] for h in health} == {
        "Payment DB", "Payment Service", "Stripe Payment API",
    }


def test_missing_team_is_404(client: TestClient) -> None:
    assert client.get("/teams/9999").status_code == 404


def test_applications_report_unowned_dependencies(client: TestClient) -> None:
    body = client.get("/applications").json()
    assert body["count"] == 4
    partner = next(a for a in body["applications"]
                   if a["application_name"] == "ShopSphere Partner API")
    # it uses Shipping Service, which has no owning team
    assert partner["unowned_components"] == 1


def test_environments_list(client: TestClient) -> None:
    body = client.get("/environments").json()
    assert body["count"] == 3
    prod = next(e for e in body["environments"] if e["environment_name"] == "Production")
    assert prod["is_production"] == 1


# ---------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------
def test_summary_matches_the_known_dataset(client: TestClient) -> None:
    s = client.get("/analytics/summary").json()
    assert s["applications"] == 4
    assert s["components"] == 19
    assert s["teams"] == 6
    assert s["developers"] == 15
    assert s["dependencies"] == 29
    assert s["deployments"] == 46
    assert s["incidents"] == 22
    assert s["open_incidents"] == 3
    assert s["unowned_components"] == 2
    assert s["critical_components"] == 8
    assert s["retired_components"] == 1
    assert s["rollbacks"] == 1


def test_high_risk_ranking_is_led_by_kafka(client: TestClient) -> None:
    body = client.get("/analytics/high-risk-components", params={"limit": 5}).json()
    assert body["components"][0]["component_name"] == "Kafka Event Bus"
    assert body["components"][0]["risk_score"] == 28
    assert "fn_risk_score" in body["scoring"]


def test_high_risk_excludes_retired_components(client: TestClient) -> None:
    body = client.get("/analytics/high-risk-components", params={"limit": 100}).json()
    names = {c["component_name"] for c in body["components"]}
    assert "Legacy Coupon Service" not in names


def test_team_health_surfaces_the_unassigned_bucket(client: TestClient) -> None:
    body = client.get("/analytics/team-health").json()
    names = {t["team_name"] for t in body["teams"]}
    assert "UNASSIGNED" in names


def test_production_infrastructure_honours_the_rollback(client: TestClient) -> None:
    """
    Inventory Service was rolled back to v3.9.0 after incident 5. The view
    must report the version actually running, not the one that was undone.
    """
    body = client.get("/analytics/production-infrastructure").json()
    inv = next(c for c in body["components"] if c["component_name"] == "Inventory Service")
    assert inv["running_version"] == "v3.9.0"


def test_unowned_components_endpoint(client: TestClient) -> None:
    body = client.get("/analytics/unowned-components").json()
    assert body["count"] == 2
    assert {c["component_name"] for c in body["components"]} == {
        "Shipping Service", "Legacy Coupon Service",
    }


def test_incident_trend_uses_window_functions(client: TestClient) -> None:
    body = client.get("/analytics/incident-trend").json()
    trend = body["trend"]
    assert len(trend) >= 6                       # several months of history
    assert trend[0]["previous_month"] is None    # LAG has nothing before the first row
    # the cumulative total must equal the overall incident count
    assert trend[-1]["cumulative_incidents"] == 22
    assert sum(t["incidents"] for t in trend) == 22


def test_correlation_is_kept_separate_from_confirmed_causation(client: TestClient) -> None:
    """
    The distinction is the point of this endpoint. Every row must be labelled,
    and the two counts must add up to the total.
    """
    body = client.get("/analytics/deploy-incident-correlation").json()
    assert body["confirmed_causes"] + body["correlations_only"] == body["count"]
    assert body["confirmed_causes"] > 0
    assert body["correlations_only"] > 0
    labels = {r["causal_status"] for r in body["results"]}
    assert labels <= {"CONFIRMED", "CORRELATION ONLY"}


def test_critical_incidents_are_sev1_or_sev2(client: TestClient) -> None:
    body = client.get("/analytics/critical-incidents", params={"limit": 200}).json()
    assert body["count"] > 0
    assert all(i["severity"] in ("SEV1", "SEV2") for i in body["incidents"])


# ---------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------
def test_graph_returns_the_whole_dependency_graph(client: TestClient) -> None:
    body = client.get("/graph").json()
    assert len(body["nodes"]) == 19
    assert len(body["edges"]) == 29
    assert body["edge_direction"] == "source depends on target"


def test_graph_edges_reference_real_nodes(client: TestClient) -> None:
    body = client.get("/graph").json()
    ids = {n["component_id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["source_id"] in ids
        assert e["target_id"] in ids


def test_graph_is_acyclic(client: TestClient) -> None:
    """Triggers prevent cycles on write; this confirms the stored data is clean."""
    body = client.get("/graph/cycles").json()
    assert body["acyclic"] is True
    assert body["cycles_found"] == 0


# ---------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------
def test_sql_injection_attempts_are_rejected_or_treated_as_data(client: TestClient) -> None:
    """
    There are TWO independent defences, and this test accepts either outcome
    per payload:

      422 - FastAPI's length validation refused the value before it reached
            the database at all.
      200 with zero rows - the value reached SQL as a bound PARAMETER and was
            compared as a literal string, matching no component_type.

    What must never happen is a 500, or a changed schema. Both are asserted
    at the end.
    """
    payloads = [
        "'; DROP TABLE component; --",
        "' OR '1'='1",
        "Database'; DELETE FROM dependency WHERE '1'='1",
        "1 UNION SELECT * FROM developer",
        # short enough to pass length validation, so this one genuinely
        # exercises the parameterised query rather than the validator
        "' OR 1=1 --",
        "Database' --",
    ]
    for p in payloads:
        r = client.get("/components", params={"component_type": p, "limit": 10})
        assert r.status_code in (200, 422), f"unexpected status for {p!r}: {r.status_code}"
        if r.status_code == 200:
            assert r.json()["count"] == 0, f"injection matched rows for {p!r}"

    # the schema and data must be completely untouched
    assert client.get("/components", params={"limit": 500}).json()["count"] == 19
    assert len(client.get("/graph").json()["edges"]) == 29
    assert client.get("/analytics/summary").json()["dependencies"] == 29


def test_numeric_aggregates_are_json_numbers_not_strings(client: TestClient) -> None:
    """
    MySQL returns SUM/AVG/window results as DECIMAL, which would serialise as
    a quoted string and force every client to guess which numbers are quoted.
    The data layer normalises them, so these must be real JSON numbers.
    """
    trend = client.get("/analytics/incident-trend").json()["trend"]
    assert isinstance(trend[-1]["cumulative_incidents"], int)

    apps = client.get("/applications").json()["applications"]
    assert all(isinstance(a["critical_components"], int) for a in apps)
    assert all(isinstance(a["unowned_components"], int) for a in apps)

    teams = client.get("/analytics/team-health").json()["teams"]
    assert all(isinstance(t["total_risk_score"], int) for t in teams)
    assert all(isinstance(t["avg_risk_score"], (int, float)) for t in teams)


def test_api_is_read_only(client: TestClient) -> None:
    """No write verb is routed. The API cannot modify the dataset."""
    for method, path in [
        ("post", "/components"),
        ("put", "/components/11"),
        ("patch", "/components/11"),
        ("delete", "/components/11"),
        ("post", "/incidents"),
        ("delete", "/incidents/1"),
    ]:
        r = getattr(client, method)(path)
        assert r.status_code in (404, 405), f"{method.upper()} {path} -> {r.status_code}"
