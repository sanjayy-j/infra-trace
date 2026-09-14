-- =====================================================================
-- InfraTrace - 08. Advanced SQL
-- File   : database/queries/08_advanced_sql.sql
--
-- Queries that need something beyond a join and a GROUP BY. Each one is
-- here because the question genuinely requires the construct, not to
-- collect SQL features.
--
-- SQL demonstrated: window functions (LAG, LEAD, ROW_NUMBER, RANK,
-- SUM/AVG OVER, FIRST_VALUE), multiple CTEs in one statement, EXISTS,
-- NOT EXISTS, correlated subqueries, NULL-safe comparison, CASE.
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
-- A1. Correlation versus confirmed causation.
--
--     This is the query that most needs care in the whole project.
--
--     The time-based rule "a production deployment happened within 7 days
--     before this incident" produces a SUSPICION. The column
--     incident.caused_by_deployment_id records a CONFIRMED cause, set only
--     after a post-incident review.
--
--     Keeping them in separate columns of the same output stops the
--     analysis from overclaiming: 14 time-correlations exist, but only 5
--     have actually been confirmed. Reporting 14 as "caused by" would be
--     wrong, and is exactly the mistake this layout prevents.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    d.version                   AS deployed_version,
    d.deployed_at,
    dev.developer_name          AS deployed_by,
    i.incident_id,
    i.severity,
    i.title,
    i.started_at                AS incident_started_at,
    ic.impact_level,
    TIMESTAMPDIFF(HOUR, d.deployed_at, i.started_at) AS hours_after_deploy,
    CASE
        WHEN i.caused_by_deployment_id = d.deployment_id
            THEN 'CONFIRMED CAUSE (post-incident review)'
        WHEN i.caused_by_deployment_id IS NOT NULL
            THEN 'a different deployment was confirmed as the cause'
        ELSE 'time correlation only - NOT established as the cause'
    END                         AS causal_status
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN component   c         ON d.component_id   = c.component_id
JOIN incident_component ic ON ic.component_id  = c.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
LEFT JOIN developer dev    ON d.deployed_by    = dev.developer_id
WHERE e.is_production = 1
  AND d.status = 'Success'
  AND i.started_at >  d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY
ORDER BY
    (i.caused_by_deployment_id = d.deployment_id) DESC,
    i.started_at DESC;


-- ---------------------------------------------------------------------
-- A2. Incident timeline per component, with the gap between incidents.
--     LAG and LEAD look backwards and forwards within each component's
--     partition. The gap between consecutive incidents tells you whether a
--     component is getting better or worse - a shrinking gap is a
--     component in decline, which no single-row query can show.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    i.incident_id,
    i.severity,
    i.started_at,
    ROW_NUMBER() OVER w                                       AS incident_number,
    LAG(i.started_at)  OVER w                                 AS previous_incident,
    LEAD(i.started_at) OVER w                                 AS next_incident,
    DATEDIFF(i.started_at, LAG(i.started_at) OVER w)          AS days_since_previous,
    FIRST_VALUE(i.severity) OVER w                            AS first_ever_severity,
    COUNT(*) OVER (PARTITION BY c.component_id)               AS total_for_component
FROM incident_component ic
JOIN incident  i ON ic.incident_id  = i.incident_id
JOIN component c ON ic.component_id = c.component_id
WINDOW w AS (PARTITION BY c.component_id ORDER BY i.started_at)
ORDER BY c.component_name, i.started_at;


-- ---------------------------------------------------------------------
-- A3. Components that are worse than average FOR THEIR OWN TYPE.
--     A correlated subquery in the HAVING clause: for each row, compare
--     against an aggregate computed over a different set (its own type).
--     Comparing a database against the global average would be
--     meaningless, because databases are structurally more depended-upon
--     than services; comparing it against other databases is not.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED')       AS owning_team,
    fn_blast_radius_count(c.component_id)     AS blast_radius,
    ROUND((SELECT AVG(fn_blast_radius_count(c2.component_id))
             FROM component c2
            WHERE c2.component_type = c.component_type
              AND c2.is_active = 1), 1)       AS avg_for_this_type,
    fn_incident_count(c.component_id)         AS incidents,
    ROUND((SELECT AVG(fn_incident_count(c3.component_id))
             FROM component c3
            WHERE c3.component_type = c.component_type
              AND c3.is_active = 1), 1)       AS avg_incidents_for_type
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
  AND fn_blast_radius_count(c.component_id) >
      (SELECT AVG(fn_blast_radius_count(c2.component_id))
         FROM component c2
        WHERE c2.component_type = c.component_type AND c2.is_active = 1)
ORDER BY c.component_type, blast_radius DESC;


-- ---------------------------------------------------------------------
-- A4. Components that changed recently but have NEVER had an incident.
--     NOT EXISTS expresses "no matching row" directly, and can stop at the
--     first match instead of building a full join and filtering NULLs.
--
--     These are the quiet ones. Either genuinely well-built, or under-
--     monitored - and the distinction matters, because a component with no
--     incident history is often just a component nobody is watching.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED')   AS owning_team,
    fn_blast_radius_count(c.component_id) AS blast_radius,
    (SELECT MAX(d.deployed_at) FROM deployment d
      WHERE d.component_id = c.component_id)  AS last_deployment,
    CASE
        WHEN fn_blast_radius_count(c.component_id) >= 3
            THEN 'no incident history but a WIDE blast radius - verify monitoring'
        ELSE 'no incident history'
    END                                   AS note
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
  AND NOT EXISTS (SELECT 1 FROM incident_component ic
                  WHERE ic.component_id = c.component_id)
  AND EXISTS (SELECT 1 FROM deployment d
              WHERE d.component_id = c.component_id)
ORDER BY blast_radius DESC, c.component_name;


-- ---------------------------------------------------------------------
-- A5. Executive summary, assembled from several CTEs.
--     Multiple named CTEs in one statement, each computing one metric,
--     then combined into a single readable report. This is the shape a
--     dashboard query takes: the CTEs keep each metric independently
--     understandable instead of hiding them in one unreadable SELECT.
-- ---------------------------------------------------------------------
WITH
estate AS (
    SELECT COUNT(*)                                                  AS components,
           SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END)             AS retired,
           SUM(CASE WHEN owner_team_id IS NULL THEN 1 ELSE 0 END)     AS unowned,
           SUM(CASE WHEN criticality = 'Critical' THEN 1 ELSE 0 END)  AS critical
    FROM component
),
graph AS (
    SELECT COUNT(*)          AS edges,
           SUM(is_critical)  AS critical_edges,
           SUM(CASE WHEN dependency_type = 'Asynchronous' THEN 1 ELSE 0 END) AS async_edges
    FROM dependency
),
incidents AS (
    SELECT COUNT(*)                                                  AS total,
           SUM(CASE WHEN status <> 'Resolved' THEN 1 ELSE 0 END)      AS open_now,
           SUM(CASE WHEN severity = 'SEV1' THEN 1 ELSE 0 END)         AS sev1,
           ROUND(AVG(TIMESTAMPDIFF(MINUTE, started_at, resolved_at)), 1) AS avg_resolution_min,
           SUM(CASE WHEN caused_by_deployment_id IS NOT NULL THEN 1 ELSE 0 END) AS deploy_caused
    FROM incident
),
releases AS (
    SELECT COUNT(*)                                              AS total,
           SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END)     AS failed,
           SUM(is_rollback)                                      AS rollbacks
    FROM deployment
),
riskiest AS (
    SELECT component_name, fn_risk_score(component_id) AS score
    FROM component WHERE is_active = 1
    ORDER BY score DESC, component_name LIMIT 1
)
SELECT 'Components'            AS metric, CAST(e.components AS CHAR)  AS value,
       CONCAT(e.critical, ' critical, ', e.unowned, ' unowned, ', e.retired, ' retired') AS detail
FROM estate e
UNION ALL
SELECT 'Dependency edges', CAST(g.edges AS CHAR),
       CONCAT(g.critical_edges, ' on the critical path, ', g.async_edges, ' asynchronous')
FROM graph g
UNION ALL
SELECT 'Applications', CAST(COUNT(*) AS CHAR), 'customer-facing and internal'
FROM application
UNION ALL
SELECT 'Teams', CAST(COUNT(*) AS CHAR),
       CONCAT((SELECT COUNT(*) FROM developer), ' engineers')
FROM team
UNION ALL
SELECT 'Incidents', CAST(i.total AS CHAR),
       CONCAT(i.open_now, ' still open, ', i.sev1, ' were SEV1, avg resolution ',
              i.avg_resolution_min, ' min')
FROM incidents i
UNION ALL
SELECT 'Incidents with a confirmed deployment cause', CAST(i.deploy_caused AS CHAR),
       CONCAT('out of ', i.total, ' - the rest had other or unconfirmed causes')
FROM incidents i
UNION ALL
SELECT 'Deployments', CAST(r.total AS CHAR),
       CONCAT(r.failed, ' failed, ', r.rollbacks, ' rollback(s)')
FROM releases r
UNION ALL
SELECT 'Highest-risk component', rk.component_name,
       CONCAT('risk score ', rk.score, ' (blast radius x criticality weight)')
FROM riskiest rk;
