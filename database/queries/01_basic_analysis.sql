-- =====================================================================
-- InfraTrace - 01. Basic inventory analysis
-- File   : database/queries/01_basic_analysis.sql
--
-- "What do we actually have?" - the questions you answer first, before any
-- risk or incident analysis makes sense.
--
-- Run:  cd database
--       mysql -u root -p --table infratrace < queries/01_basic_analysis.sql
--
-- SQL demonstrated here: JOIN, LEFT JOIN, GROUP BY, aggregates, CASE,
-- correlated scalar subqueries.
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
-- B1. Platform-wide inventory, grouped by component type.
--     One table answering "how big is the estate, how connected is it,
--     and where does it hurt?"
--
--     Design note: the counts come from correlated scalar subqueries
--     rather than from four LEFT JOINs. Joining dependency, incident and
--     deployment in one query MULTIPLIES rows (a component with 3
--     dependencies and 4 deployments produces 12 rows), which silently
--     inflates every SUM. Counting each fact in its own subquery keeps
--     the totals correct. This query originally had that exact bug and
--     reported 182 critical components instead of 8.
-- ---------------------------------------------------------------------
SELECT
    c.component_type,
    COUNT(*)                                                    AS components,
    SUM(CASE WHEN c.owner_team_id IS NULL THEN 1 ELSE 0 END)    AS unowned,
    SUM(CASE WHEN c.criticality = 'Critical' THEN 1 ELSE 0 END) AS critical,
    SUM(CASE WHEN c.is_active = 0 THEN 1 ELSE 0 END)            AS retired,

    SUM((SELECT COUNT(*) FROM dependency d
          WHERE d.component_id = c.component_id))               AS outgoing_dependencies,

    SUM((SELECT COUNT(*) FROM dependency d
          WHERE d.depends_on_id = c.component_id))              AS incoming_dependencies,

    (SELECT COUNT(DISTINCT ic.incident_id)
       FROM incident_component ic
       JOIN component c2 ON ic.component_id = c2.component_id
      WHERE c2.component_type = c.component_type)               AS distinct_incidents,

    SUM((SELECT COUNT(*) FROM deployment dp
          WHERE dp.component_id = c.component_id))              AS deployments_recorded
FROM component c
GROUP BY c.component_type
ORDER BY components DESC, c.component_type;


-- ---------------------------------------------------------------------
-- B2. The full component catalogue.
--     The reference list an engineer scans to find a component and see
--     who owns it, how exposed it is and whether it is still live.
-- ---------------------------------------------------------------------
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    CASE WHEN c.is_active = 1 THEN 'active' ELSE 'retired' END AS state,
    COALESCE(t.team_name, 'UNASSIGNED')                        AS owning_team,
    c.tech_stack,
    fn_direct_dependent_count(c.component_id)                  AS dependents,
    fn_dependency_depth(c.component_id)                        AS dependency_depth
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
ORDER BY
    FIELD(c.criticality, 'Critical', 'High', 'Medium', 'Low'),
    c.component_name;


-- ---------------------------------------------------------------------
-- B3. What infrastructure exists in each environment?
--     A component is "present" in an environment if it has ever been
--     deployed there. The gap between Staging and Production counts is
--     the set of things built but never shipped.
-- ---------------------------------------------------------------------
SELECT
    e.environment_name,
    e.region,
    CASE WHEN e.is_production = 1 THEN 'yes' ELSE 'no' END AS is_production,
    COUNT(DISTINCT d.component_id)                         AS distinct_components,
    COUNT(d.deployment_id)                                 AS total_deployments,
    SUM(CASE WHEN d.status = 'Failed' THEN 1 ELSE 0 END)   AS failed_deployments,
    MIN(d.deployed_at)                                     AS first_deployment,
    MAX(d.deployed_at)                                     AS latest_deployment
FROM environment e
LEFT JOIN deployment d ON e.environment_id = d.environment_id
GROUP BY e.environment_id, e.environment_name, e.region, e.is_production
ORDER BY e.is_production DESC, e.environment_name;


-- ---------------------------------------------------------------------
-- B4. Team roster and workload.
--     Headcount next to what each team is responsible for. A team owning
--     many critical components with few engineers is a staffing risk.
--     LEFT JOIN so a team with no developers or no components still
--     appears - those are the rows worth noticing.
-- ---------------------------------------------------------------------
SELECT
    t.team_name,
    t.team_email,
    COUNT(DISTINCT d.developer_id)                                   AS developers,
    COUNT(DISTINCT c.component_id)                                   AS components_owned,
    COUNT(DISTINCT CASE WHEN c.criticality = 'Critical'
                        THEN c.component_id END)                     AS critical_owned,
    COUNT(DISTINCT a.application_id)                                 AS applications_owned,
    ROUND(COUNT(DISTINCT c.component_id)
          / NULLIF(COUNT(DISTINCT d.developer_id), 0), 2)            AS components_per_developer
FROM team t
LEFT JOIN developer   d ON t.team_id = d.team_id
LEFT JOIN component   c ON t.team_id = c.owner_team_id
LEFT JOIN application a ON t.team_id = a.owner_team_id
GROUP BY t.team_id, t.team_name, t.team_email
ORDER BY critical_owned DESC, components_owned DESC, t.team_name;
