-- =====================================================================
-- InfraTrace - Views
-- File   : database/views/views.sql
-- Purpose: Reusable, readable projections over the normalised tables so
--          the common analytical questions do not need repeated joins.
-- =====================================================================

-- Make the connection character set AND COLLATION explicit.
--
-- The mysql client otherwise derives them from the host platform - cp850 on a
-- Windows console, latin1 on Linux - and neither matches this database. Mixing
-- a cp850 literal with utf8mb4 data raises "ERROR 1271 Illegal mix of
-- collations", so without this the scripts run on Linux and fail on Windows.
--
-- The COLLATE clause matters as much as the charset. This database is
-- utf8mb4_unicode_ci, while the MySQL 8 server default is utf8mb4_0900_ai_ci.
-- Plain "SET NAMES utf8mb4" would leave literals and CAST(... AS CHAR) results
-- in utf8mb4_0900_ai_ci and column data in utf8mb4_unicode_ci - two collations
-- of the same charset at equal coercibility, which UNION and CONCAT_WS reject
-- with the same error 1271. Naming the collation aligns all three.
SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci;

USE infratrace;

DROP VIEW IF EXISTS component_ownership_view;
DROP VIEW IF EXISTS production_infrastructure_view;
DROP VIEW IF EXISTS incident_impact_view;
DROP VIEW IF EXISTS dependency_edge_view;

-- ---------------------------------------------------------------------
-- 1. component_ownership_view
--    Every component with its type, criticality and owning team.
--    Unowned components appear with 'UNASSIGNED' so they stay visible.
--    LEFT JOIN is required - an inner join would hide exactly the rows
--    an ownership audit is looking for.
-- ---------------------------------------------------------------------
CREATE VIEW component_ownership_view AS
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    c.is_active,
    COALESCE(t.team_name, 'UNASSIGNED')  AS owning_team,
    COALESCE(t.team_email, 'none')       AS owner_contact
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id;

-- ---------------------------------------------------------------------
-- 2. production_infrastructure_view
--    What is actually running in production right now: for each
--    component, its most recent successful production deployment.
--    The correlated subquery picks the latest deployed_at per component.
--
--    DELIBERATELY NOT FILTERED BY component.is_active.
--
--    is_active is a CATALOGUE state ("we consider this component retired"),
--    while deployment rows are HISTORY ("this version went out at this time").
--    The schema has no undeploy or decommission event, so marking a component
--    retired does not remove it from the servers it is running on. A component
--    retired AFTER being deployed is therefore still reported here - which is
--    the operationally useful answer, because that is a thing still running
--    that nobody owns any more.
--
--    The related rule that IS enforced is the opposite direction: a retired
--    component can never ACQUIRE a new production deployment. That is a write
--    rule, so it lives in trg_deployment_validate_insert/_update, not here.
--
--    In the shipped dataset the only retired component has never reached
--    production, so this distinction is invisible until you retire something
--    that is live. tests/sql/04_stored_programs.sql asserts the invariant the
--    view really provides, rather than one it only appears to provide.
-- ---------------------------------------------------------------------
CREATE VIEW production_infrastructure_view AS
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED') AS owning_team,
    e.environment_name,
    e.region,
    d.version           AS running_version,
    d.deployed_at       AS deployed_at,
    dev.developer_name  AS deployed_by
FROM deployment d
JOIN component   c   ON d.component_id   = c.component_id
JOIN environment e   ON d.environment_id = e.environment_id
LEFT JOIN team   t   ON c.owner_team_id  = t.team_id
LEFT JOIN developer dev ON d.deployed_by = dev.developer_id
WHERE e.is_production = 1
  AND d.status = 'Success'
  AND d.deployed_at = (
        SELECT MAX(d2.deployed_at)
        FROM deployment d2
        WHERE d2.component_id   = d.component_id
          AND d2.environment_id = d.environment_id
          AND d2.status = 'Success'
  );

-- ---------------------------------------------------------------------
-- 3. incident_impact_view
--    One row per incident/affected-component pair, together with the
--    team that has to respond. This is the backbone of most incident
--    reporting queries.
-- ---------------------------------------------------------------------
CREATE VIEW incident_impact_view AS
SELECT
    i.incident_id,
    i.title,
    i.severity,
    i.status            AS incident_status,
    i.started_at,
    i.resolved_at,
    e.environment_name,
    c.component_id,
    c.component_name,
    c.component_type,
    ic.impact_level,
    COALESCE(t.team_name, 'UNASSIGNED') AS responsible_team,
    TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at) AS resolution_minutes
FROM incident i
JOIN incident_component ic ON i.incident_id     = ic.incident_id
JOIN component c           ON ic.component_id   = c.component_id
JOIN environment e         ON i.environment_id  = e.environment_id
LEFT JOIN team t           ON c.owner_team_id   = t.team_id;

-- ---------------------------------------------------------------------
-- 4. dependency_edge_view
--    The dependency graph in human-readable form. Resolves both ends of
--    the self-referencing relationship to component names, so the graph
--    can be read without joining component twice every time.
-- ---------------------------------------------------------------------
CREATE VIEW dependency_edge_view AS
SELECT
    d.dependency_id,
    src.component_id    AS component_id,
    src.component_name  AS component_name,
    src.component_type  AS component_type,
    tgt.component_id    AS depends_on_id,
    tgt.component_name  AS depends_on_name,
    tgt.component_type  AS depends_on_type,
    d.dependency_type,
    d.is_critical,
    d.description
FROM dependency d
JOIN component src ON d.component_id  = src.component_id
JOIN component tgt ON d.depends_on_id = tgt.component_id;

SELECT 'Views created: 4.' AS status;
