-- =====================================================================
-- InfraTrace - 05. Deployment analysis
-- File   : database/queries/05_deployment_analysis.sql
--
-- What is running where, how often it changes, and how often changes fail.
--
-- SQL demonstrated: views, GROUP BY, CASE, window functions (LAG,
-- ROW_NUMBER, COUNT OVER), correlated subqueries, HAVING.
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

-- ---------------------------------------------------------------------
-- P1. What is running in production right now?
--     Uses production_infrastructure_view, which resolves the latest
--     SUCCESSFUL production deployment per component.
--
--     Note this correctly honours rollbacks. Inventory Service shows
--     v3.9.0, not the v4.0.0 that was rolled back after incident 5,
--     because the rollback is itself a successful later deployment.
-- ---------------------------------------------------------------------
SELECT
    component_name,
    component_type,
    criticality,
    owning_team,
    running_version,
    deployed_at,
    deployed_by
FROM production_infrastructure_view
ORDER BY
    FIELD(criticality, 'Critical', 'High', 'Medium', 'Low'),
    component_name;


-- ---------------------------------------------------------------------
-- P2. Deployment activity and health, by component and environment.
--     A component that only ever reaches Development has never shipped.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    e.environment_name,
    COUNT(*)                                                 AS total_deployments,
    SUM(CASE WHEN d.status = 'Success' THEN 1 ELSE 0 END)     AS successful,
    SUM(CASE WHEN d.status = 'Failed'  THEN 1 ELSE 0 END)     AS failed,
    SUM(d.is_rollback)                                       AS rollbacks,
    ROUND(100.0 * SUM(CASE WHEN d.status = 'Success' THEN 1 ELSE 0 END)
          / COUNT(*), 1)                                     AS success_rate_pct,
    MAX(d.deployed_at)                                       AS last_deployed_at
FROM deployment d
JOIN component   c ON d.component_id   = c.component_id
JOIN environment e ON d.environment_id = e.environment_id
GROUP BY c.component_id, c.component_name, e.environment_id, e.environment_name
ORDER BY c.component_name, e.environment_name;


-- ---------------------------------------------------------------------
-- P3. Production release timeline, with the previous version alongside.
--     LAG() reaches back to the preceding row within each component's
--     partition, giving "what was running before this release" and how
--     long the previous version survived - without a self-join.
--
--     PARTITION BY restarts the window at each component, so one
--     component's history never bleeds into another's.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    d.version                                                   AS deployed_version,
    LAG(d.version) OVER w                                       AS previous_version,
    d.deployed_at,
    LAG(d.deployed_at) OVER w                                    AS previous_deployed_at,
    TIMESTAMPDIFF(DAY, LAG(d.deployed_at) OVER w, d.deployed_at) AS days_since_previous,
    ROW_NUMBER() OVER w                                          AS release_number,
    CASE WHEN d.is_rollback = 1 THEN 'ROLLBACK' ELSE '' END      AS note,
    dev.developer_name                                           AS deployed_by
FROM deployment d
JOIN component c        ON d.component_id = c.component_id
JOIN environment e      ON d.environment_id = e.environment_id
LEFT JOIN developer dev ON d.deployed_by = dev.developer_id
WHERE e.is_production = 1 AND d.status = 'Success'
WINDOW w AS (PARTITION BY d.component_id ORDER BY d.deployed_at)
ORDER BY c.component_name, d.deployed_at;


-- ---------------------------------------------------------------------
-- P4. Where is the deployment process itself unhealthy?
--     Failures and rollbacks are process signals, not component signals.
--     A component with a rollback shipped something that had to be undone;
--     a component with failures cannot get out of the door.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    COALESCE(t.team_name, 'UNASSIGNED')                      AS owning_team,
    COUNT(*)                                                 AS total_deployments,
    SUM(CASE WHEN d.status = 'Failed' THEN 1 ELSE 0 END)     AS failed,
    SUM(d.is_rollback)                                       AS rollbacks,
    -- how many incidents followed a production deployment of this
    -- component within 7 days: a correlated subquery, evaluated per row
    (SELECT COUNT(DISTINCT i.incident_id)
       FROM deployment d2
       JOIN environment e2        ON d2.environment_id = e2.environment_id
       JOIN incident_component ic ON ic.component_id   = d2.component_id
       JOIN incident i            ON ic.incident_id    = i.incident_id
      WHERE d2.component_id = c.component_id
        AND e2.is_production = 1
        AND d2.status = 'Success'
        AND i.started_at >  d2.deployed_at
        AND i.started_at <= d2.deployed_at + INTERVAL 7 DAY) AS incidents_after_release,
    CASE
        WHEN SUM(d.is_rollback) > 0 THEN 'shipped a change that had to be undone'
        WHEN SUM(CASE WHEN d.status = 'Failed' THEN 1 ELSE 0 END) > 0
             THEN 'deployment pipeline failing'
        ELSE 'clean'
    END                                                      AS assessment
FROM deployment d
JOIN component c ON d.component_id = c.component_id
LEFT JOIN team t ON c.owner_team_id = t.team_id
GROUP BY c.component_id, c.component_name, t.team_name
HAVING failed > 0 OR rollbacks > 0 OR incidents_after_release > 0
ORDER BY rollbacks DESC, failed DESC, incidents_after_release DESC;


-- ---------------------------------------------------------------------
-- P5. Promotion pipeline: how far has each component travelled?
--     Does every component follow Development -> Staging -> Production?
--     A component live in production that never passed through staging
--     is a process gap worth knowing about.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.criticality,
    MAX(CASE WHEN e.environment_name = 'Development' THEN 'yes' ELSE '-' END) AS in_dev,
    MAX(CASE WHEN e.environment_name = 'Staging'     THEN 'yes' ELSE '-' END) AS in_staging,
    MAX(CASE WHEN e.environment_name = 'Production'  THEN 'yes' ELSE '-' END) AS in_production,
    CASE
        WHEN MAX(CASE WHEN e.is_production = 1 THEN 1 ELSE 0 END) = 1
         AND MAX(CASE WHEN e.environment_name = 'Staging' THEN 1 ELSE 0 END) = 0
            THEN 'IN PRODUCTION WITHOUT PASSING STAGING'
        WHEN MAX(CASE WHEN e.is_production = 1 THEN 1 ELSE 0 END) = 0
            THEN 'never reached production'
        ELSE 'normal promotion path'
    END                                                                      AS pipeline_note
FROM component c
JOIN deployment d  ON c.component_id = d.component_id
JOIN environment e ON d.environment_id = e.environment_id
GROUP BY c.component_id, c.component_name, c.criticality
ORDER BY pipeline_note, c.component_name;
