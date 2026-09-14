-- =====================================================================
-- InfraTrace - 04. Incident analysis
-- File   : database/queries/04_incident_analysis.sql
--
-- What has actually broken, how badly, how often, and who had to fix it.
--
-- A note on "recent": these queries measure recency against the latest
-- event already in the dataset - (SELECT MAX(started_at) FROM incident) -
-- instead of against CURDATE(). A fixed sample dataset compared with a
-- moving system clock would silently start returning nothing.
--
-- SQL demonstrated: JOIN, GROUP BY, HAVING, aggregates, GROUP_CONCAT with
-- ORDER BY, CASE, window functions (SUM OVER, LAG), views.
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
-- I1. The last 30 days of incidents, and what each one affected.
--     The impact_level ordering inside GROUP_CONCAT puts the root cause
--     first, so each row reads as a one-line incident summary.
-- ---------------------------------------------------------------------
SELECT
    i.incident_id,
    i.severity,
    i.status,
    i.title,
    e.environment_name,
    i.started_at,
    TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at) AS resolution_minutes,
    GROUP_CONCAT(
        CONCAT(c.component_name, ' (', ic.impact_level, ')')
        ORDER BY FIELD(ic.impact_level, 'RootCause', 'Unavailable', 'Degraded', 'Minor')
        SEPARATOR ', '
    ) AS affected_components
FROM incident i
JOIN incident_component ic ON i.incident_id    = ic.incident_id
JOIN component c           ON ic.component_id  = c.component_id
JOIN environment e         ON i.environment_id = e.environment_id
WHERE i.started_at >= (SELECT MAX(started_at) FROM incident) - INTERVAL 30 DAY
GROUP BY i.incident_id, i.severity, i.status, i.title,
         e.environment_name, i.started_at, i.resolved_at
ORDER BY i.started_at DESC;


-- ---------------------------------------------------------------------
-- I2. Severity breakdown and mean time to resolution.
--     AVG ignores NULL, so unresolved incidents do not drag the average
--     towards zero - they are counted separately in still_open. Treating
--     an open incident as a zero-minute resolution would be the classic
--     way to make this metric lie.
-- ---------------------------------------------------------------------
SELECT
    i.severity,
    COUNT(*)                                                        AS total_incidents,
    SUM(CASE WHEN i.status = 'Resolved' THEN 1 ELSE 0 END)          AS resolved,
    SUM(CASE WHEN i.status <> 'Resolved' THEN 1 ELSE 0 END)         AS still_open,
    ROUND(AVG(TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at)), 1)
                                                                    AS avg_resolution_minutes,
    MIN(TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at))         AS fastest_minutes,
    MAX(TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at))         AS slowest_minutes,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM incident), 1)    AS pct_of_all_incidents
FROM incident i
GROUP BY i.severity
ORDER BY FIELD(i.severity, 'SEV1', 'SEV2', 'SEV3', 'SEV4');


-- ---------------------------------------------------------------------
-- I3. Which components keep failing?
--     Split by whether the component was the root cause or collateral
--     damage - a component that is repeatedly the ROOT CAUSE needs
--     engineering work, while one that is repeatedly DEGRADED is usually
--     just downstream of something else.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.component_type,
    COALESCE(t.team_name, 'UNASSIGNED')                                AS owning_team,
    COUNT(ic.incident_id)                                              AS incident_count,
    SUM(CASE WHEN ic.impact_level = 'RootCause' THEN 1 ELSE 0 END)      AS times_root_cause,
    SUM(CASE WHEN ic.impact_level IN ('Degraded','Minor','Unavailable')
             THEN 1 ELSE 0 END)                                        AS times_collateral,
    SUM(CASE WHEN i.severity = 'SEV1' THEN 1 ELSE 0 END)               AS sev1_incidents,
    MAX(i.started_at)                                                  AS most_recent_incident,
    CASE
        WHEN SUM(CASE WHEN ic.impact_level = 'RootCause' THEN 1 ELSE 0 END) >= 2
            THEN 'REPEAT ROOT CAUSE - needs engineering work'
        WHEN SUM(CASE WHEN ic.impact_level = 'RootCause' THEN 1 ELSE 0 END) = 0
            THEN 'always downstream - fix its dependencies'
        ELSE 'mixed'
    END                                                                AS assessment
FROM component c
JOIN incident_component ic ON c.component_id = ic.component_id
JOIN incident i            ON ic.incident_id = i.incident_id
LEFT JOIN team t           ON c.owner_team_id = t.team_id
GROUP BY c.component_id, c.component_name, c.component_type, t.team_name
HAVING incident_count > 1
ORDER BY incident_count DESC, times_root_cause DESC, c.component_name;


-- ---------------------------------------------------------------------
-- I4. Which teams are accountable for SEV1 incidents?
--     Answers "who needs to be in the post-incident review?".
--     Uses incident_impact_view to avoid repeating the four-way join.
-- ---------------------------------------------------------------------
SELECT
    v.responsible_team,
    COUNT(DISTINCT v.incident_id)     AS sev1_incidents_involved,
    COUNT(DISTINCT v.component_id)    AS components_involved,
    SUM(CASE WHEN v.impact_level = 'RootCause' THEN 1 ELSE 0 END) AS root_cause_count,
    ROUND(AVG(v.resolution_minutes), 1) AS avg_resolution_minutes,
    GROUP_CONCAT(DISTINCT v.component_name ORDER BY v.component_name SEPARATOR ', ')
                                      AS components
FROM incident_impact_view v
WHERE v.severity = 'SEV1'
GROUP BY v.responsible_team
ORDER BY sev1_incidents_involved DESC, root_cause_count DESC;


-- ---------------------------------------------------------------------
-- I5. Incident trend, month by month.
--     Window functions rather than a self-join:
--       SUM(...) OVER (ORDER BY month)  - running cumulative total
--       LAG(...) OVER (ORDER BY month)  - the previous month's value, so
--                                         month-on-month change is a
--                                         simple subtraction
--     A GROUP BY alone cannot do this: it collapses rows, whereas a
--     window function keeps every row and computes ACROSS them.
-- ---------------------------------------------------------------------
SELECT
    month_start,
    incidents,
    sev1,
    SUM(incidents) OVER (ORDER BY month_start)            AS cumulative_incidents,
    LAG(incidents) OVER (ORDER BY month_start)            AS previous_month,
    incidents - LAG(incidents) OVER (ORDER BY month_start) AS change_vs_previous,
    ROUND(AVG(incidents) OVER (ORDER BY month_start
                               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 1)
                                                          AS three_month_moving_avg,
    ROUND(avg_resolution, 1)                              AS avg_resolution_minutes
FROM (
    SELECT
        DATE_FORMAT(i.started_at, '%Y-%m-01')                       AS month_start,
        COUNT(*)                                                    AS incidents,
        SUM(CASE WHEN i.severity = 'SEV1' THEN 1 ELSE 0 END)        AS sev1,
        AVG(TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at))     AS avg_resolution
    FROM incident i
    GROUP BY DATE_FORMAT(i.started_at, '%Y-%m-01')
) monthly
ORDER BY month_start;


-- ---------------------------------------------------------------------
-- I6. Who is carrying the on-call load?
--     Incidents reported per engineer, and how severe they were.
--     LEFT JOIN from developer so engineers who have reported nothing
--     still appear - useful for spotting an unevenly shared rotation.
-- ---------------------------------------------------------------------
SELECT
    dev.developer_name,
    dev.job_role,
    COALESCE(t.team_name, 'UNASSIGNED')                          AS team,
    COUNT(i.incident_id)                                         AS incidents_reported,
    SUM(CASE WHEN i.severity = 'SEV1' THEN 1 ELSE 0 END)         AS sev1_reported,
    COUNT(DISTINCT dp.deployment_id)                             AS deployments_performed,
    MAX(i.started_at)                                            AS last_incident_reported
FROM developer dev
LEFT JOIN team t       ON dev.team_id      = t.team_id
LEFT JOIN incident i   ON dev.developer_id = i.reported_by
LEFT JOIN deployment dp ON dev.developer_id = dp.deployed_by
GROUP BY dev.developer_id, dev.developer_name, dev.job_role, t.team_name
ORDER BY incidents_reported DESC, deployments_performed DESC, dev.developer_name;
