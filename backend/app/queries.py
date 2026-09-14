"""
Every SQL statement the API runs, in one place.

Keeping the SQL here rather than scattered through the route handlers means
the database contract is reviewable on its own, and it stays obvious that
the analysis is done by the database - views, stored functions and
procedures - rather than in Python.

Every statement that takes input uses %s placeholders. There is no string
formatting of user data anywhere in this file.
"""

# ---------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------

COMPONENTS_LIST = """
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    c.tech_stack,
    c.is_active,
    c.owner_team_id,
    COALESCE(t.team_name, 'UNASSIGNED')       AS owning_team,
    fn_direct_dependent_count(c.component_id) AS direct_dependents,
    fn_blast_radius_count(c.component_id)     AS blast_radius,
    fn_dependency_depth(c.component_id)       AS dependency_depth,
    fn_incident_count(c.component_id)         AS incident_count,
    fn_risk_score(c.component_id)             AS risk_score
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE (%s IS NULL OR c.component_type = %s)
  AND (%s IS NULL OR c.criticality    = %s)
  AND (%s IS NULL OR c.is_active      = %s)
ORDER BY fn_risk_score(c.component_id) DESC, c.component_name
LIMIT %s OFFSET %s
"""

COMPONENT_BY_ID = """
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    c.tech_stack,
    c.is_active,
    c.created_on,
    c.owner_team_id,
    COALESCE(t.team_name, 'UNASSIGNED')       AS owning_team,
    t.team_email                              AS owner_contact,
    fn_direct_dependent_count(c.component_id) AS direct_dependents,
    fn_blast_radius_count(c.component_id)     AS blast_radius,
    fn_dependency_depth(c.component_id)       AS dependency_depth,
    fn_incident_count(c.component_id)         AS incident_count,
    fn_risk_score(c.component_id)             AS risk_score
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.component_id = %s
"""

# Direct dependencies: what this component needs.
COMPONENT_DEPENDENCIES = """
SELECT
    tgt.component_id,
    tgt.component_name,
    tgt.component_type,
    tgt.criticality,
    d.dependency_type,
    d.is_critical,
    d.description,
    COALESCE(t.team_name, 'UNASSIGNED') AS owning_team
FROM dependency d
JOIN component tgt ON d.depends_on_id = tgt.component_id
LEFT JOIN team t   ON tgt.owner_team_id = t.team_id
WHERE d.component_id = %s
ORDER BY d.is_critical DESC, tgt.component_name
"""

# Direct dependents: what needs this component.
COMPONENT_DEPENDENTS = """
SELECT
    src.component_id,
    src.component_name,
    src.component_type,
    src.criticality,
    d.dependency_type,
    d.is_critical,
    d.description,
    COALESCE(t.team_name, 'UNASSIGNED') AS owning_team
FROM dependency d
JOIN component src ON d.component_id = src.component_id
LEFT JOIN team t   ON src.owner_team_id = t.team_id
WHERE d.depends_on_id = %s
ORDER BY d.is_critical DESC, src.component_name
"""

COMPONENT_INCIDENTS = """
SELECT
    i.incident_id,
    i.title,
    i.severity,
    i.status,
    i.started_at,
    i.resolved_at,
    i.root_cause,
    i.caused_by_deployment_id,
    ic.impact_level,
    e.environment_name,
    TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at) AS resolution_minutes
FROM incident_component ic
JOIN incident i    ON ic.incident_id = i.incident_id
JOIN environment e ON i.environment_id = e.environment_id
WHERE ic.component_id = %s
ORDER BY i.started_at DESC
"""

COMPONENT_DEPLOYMENTS = """
SELECT
    d.deployment_id,
    d.version,
    d.deployed_at,
    d.status,
    d.is_rollback,
    e.environment_name,
    e.is_production,
    dev.developer_name AS deployed_by
FROM deployment d
JOIN environment e      ON d.environment_id = e.environment_id
LEFT JOIN developer dev ON d.deployed_by = dev.developer_id
WHERE d.component_id = %s
ORDER BY d.deployed_at DESC
"""

# Applications reached through the blast radius of a component.
# The recursive CTE is the database doing the graph work, not Python.
COMPONENT_AFFECTED_APPLICATIONS = """
WITH RECURSIVE impact (component_id) AS (
    SELECT %s
    UNION
    SELECT d.component_id
    FROM dependency d
    JOIN impact im ON d.depends_on_id = im.component_id
)
SELECT
    a.application_id,
    a.application_name,
    a.app_type,
    COUNT(DISTINCT c.component_id) AS affected_components_used,
    GROUP_CONCAT(DISTINCT c.component_name ORDER BY c.component_name SEPARATOR ', ')
                                   AS via_components
FROM impact im
JOIN component c              ON im.component_id   = c.component_id
JOIN application_component ac ON c.component_id    = ac.component_id
JOIN application a            ON ac.application_id = a.application_id
GROUP BY a.application_id, a.application_name, a.app_type
ORDER BY affected_components_used DESC, a.application_name
"""

# Teams that would need paging, with the nearest affected component.
COMPONENT_AFFECTED_TEAMS = """
WITH RECURSIVE impact (component_id, depth) AS (
    SELECT %s, 0
    UNION
    SELECT d.component_id, im.depth + 1
    FROM dependency d
    JOIN impact im ON d.depends_on_id = im.component_id
    WHERE im.depth < 1000
)
SELECT
    c.owner_team_id,
    COALESCE(t.team_name, 'UNASSIGNED') AS team_name,
    COALESCE(t.team_email, '')          AS team_email,
    MIN(im.depth)                       AS nearest_hop,
    COUNT(DISTINCT c.component_id)      AS components_affected,
    GROUP_CONCAT(DISTINCT c.component_name ORDER BY c.component_name SEPARATOR ', ')
                                        AS their_components
FROM impact im
JOIN component c ON im.component_id = c.component_id
LEFT JOIN team t ON c.owner_team_id = t.team_id
GROUP BY c.owner_team_id, t.team_name, t.team_email
ORDER BY nearest_hop, components_affected DESC
"""

# Propagation paths, for the graph visualisation.
COMPONENT_BLAST_PATHS = """
WITH RECURSIVE impact_path (component_id, depth, path) AS (
    SELECT component_id, 0, CAST(component_name AS CHAR(1000))
    FROM component WHERE component_id = %s
    UNION ALL
    SELECT d.component_id, ip.depth + 1,
           CONCAT(ip.path, ' -> ', c.component_name)
    FROM dependency d
    JOIN impact_path ip ON d.depends_on_id = ip.component_id
    JOIN component   c  ON d.component_id  = c.component_id
    WHERE ip.depth < 1000
)
SELECT depth AS hops, path AS propagation_path
FROM impact_path
WHERE depth > 0
ORDER BY depth, path
"""

# Edges within the blast radius, so the frontend can draw the subgraph.
COMPONENT_BLAST_EDGES = """
WITH RECURSIVE impact (component_id) AS (
    SELECT %s
    UNION
    SELECT d.component_id
    FROM dependency d
    JOIN impact im ON d.depends_on_id = im.component_id
)
SELECT
    d.component_id   AS source_id,
    src.component_name AS source_name,
    d.depends_on_id  AS target_id,
    tgt.component_name AS target_name,
    d.dependency_type,
    d.is_critical
FROM dependency d
JOIN component src ON d.component_id  = src.component_id
JOIN component tgt ON d.depends_on_id = tgt.component_id
WHERE d.component_id  IN (SELECT component_id FROM impact)
  AND d.depends_on_id IN (SELECT component_id FROM impact)
ORDER BY src.component_name, tgt.component_name
"""

# ---------------------------------------------------------------------
# Teams, applications, environments
# ---------------------------------------------------------------------

# Every count comes from its own scalar subquery. Joining developer,
# component and application together would multiply rows (a team with
# 3 developers and 4 components yields 12) and inflate every aggregate -
# the same fan-out bug that once made the platform summary report 182
# critical components instead of 8.
TEAMS_LIST = """
SELECT
    t.team_id,
    t.team_name,
    t.team_email,
    t.created_on,
    (SELECT COUNT(*) FROM developer dev
      WHERE dev.team_id = t.team_id)                          AS developers,
    (SELECT COUNT(*) FROM component c
      WHERE c.owner_team_id = t.team_id)                      AS components_owned,
    (SELECT COUNT(*) FROM component c
      WHERE c.owner_team_id = t.team_id
        AND c.criticality = 'Critical')                       AS critical_owned,
    (SELECT COUNT(*) FROM application a
      WHERE a.owner_team_id = t.team_id)                      AS applications_owned,
    COALESCE((SELECT SUM(fn_risk_score(c.component_id)) FROM component c
               WHERE c.owner_team_id = t.team_id
                 AND c.is_active = 1), 0)                     AS total_risk_score,
    COALESCE((SELECT SUM(fn_incident_count(c.component_id)) FROM component c
               WHERE c.owner_team_id = t.team_id), 0)         AS incident_involvements
FROM team t
ORDER BY total_risk_score DESC, t.team_name
"""

TEAM_BY_ID = """
SELECT t.team_id, t.team_name, t.team_email, t.created_on
FROM team t WHERE t.team_id = %s
"""

TEAM_DEVELOPERS = """
SELECT developer_id, developer_name, email, job_role, joined_on
FROM developer WHERE team_id = %s
ORDER BY developer_name
"""

APPLICATIONS_LIST = """
SELECT
    a.application_id,
    a.application_name,
    a.app_type,
    a.description,
    COALESCE(t.team_name, 'UNASSIGNED')                         AS owning_team,
    COUNT(ac.component_id)                                      AS components_used,
    SUM(CASE WHEN c.criticality = 'Critical' THEN 1 ELSE 0 END)  AS critical_components,
    SUM(CASE WHEN c.owner_team_id IS NULL THEN 1 ELSE 0 END)     AS unowned_components
FROM application a
LEFT JOIN team t               ON a.owner_team_id  = t.team_id
LEFT JOIN application_component ac ON a.application_id = ac.application_id
LEFT JOIN component c          ON ac.component_id  = c.component_id
GROUP BY a.application_id, a.application_name, a.app_type, a.description, t.team_name
ORDER BY components_used DESC, a.application_name
"""

APPLICATION_COMPONENTS = """
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED') AS owning_team,
    ac.usage_notes,
    fn_risk_score(c.component_id)       AS risk_score
FROM application_component ac
JOIN component c ON ac.component_id = c.component_id
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE ac.application_id = %s
ORDER BY fn_risk_score(c.component_id) DESC, c.component_name
"""

ENVIRONMENTS_LIST = """
SELECT
    e.environment_id,
    e.environment_name,
    e.region,
    e.is_production,
    COUNT(DISTINCT d.component_id) AS distinct_components,
    COUNT(d.deployment_id)         AS total_deployments
FROM environment e
LEFT JOIN deployment d ON e.environment_id = d.environment_id
GROUP BY e.environment_id, e.environment_name, e.region, e.is_production
ORDER BY e.is_production DESC, e.environment_name
"""

# ---------------------------------------------------------------------
# Incidents and deployments
# ---------------------------------------------------------------------

INCIDENTS_LIST = """
SELECT
    i.incident_id,
    i.title,
    i.severity,
    i.status,
    i.started_at,
    i.resolved_at,
    i.root_cause,
    i.caused_by_deployment_id,
    e.environment_name,
    dev.developer_name AS reported_by,
    TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at) AS resolution_minutes,
    (SELECT GROUP_CONCAT(CONCAT(c.component_name, ' (', ic.impact_level, ')')
             ORDER BY FIELD(ic.impact_level,'RootCause','Unavailable','Degraded','Minor')
             SEPARATOR ', ')
       FROM incident_component ic
       JOIN component c ON ic.component_id = c.component_id
      WHERE ic.incident_id = i.incident_id) AS affected_components,
    (SELECT c.component_name
       FROM incident_component ic
       JOIN component c ON ic.component_id = c.component_id
      WHERE ic.incident_id = i.incident_id AND ic.impact_level = 'RootCause'
      LIMIT 1)                             AS root_cause_component
FROM incident i
JOIN environment e      ON i.environment_id = e.environment_id
LEFT JOIN developer dev ON i.reported_by    = dev.developer_id
WHERE (%s IS NULL OR i.severity = %s)
  AND (%s IS NULL OR i.status   = %s)
ORDER BY i.started_at DESC
LIMIT %s OFFSET %s
"""

INCIDENT_BY_ID = """
SELECT
    i.incident_id, i.title, i.severity, i.status, i.started_at, i.resolved_at,
    i.root_cause, i.caused_by_deployment_id, e.environment_name,
    dev.developer_name AS reported_by,
    TIMESTAMPDIFF(MINUTE, i.started_at, i.resolved_at) AS resolution_minutes
FROM incident i
JOIN environment e      ON i.environment_id = e.environment_id
LEFT JOIN developer dev ON i.reported_by    = dev.developer_id
WHERE i.incident_id = %s
"""

INCIDENT_COMPONENTS = """
SELECT
    c.component_id, c.component_name, c.component_type, c.criticality,
    ic.impact_level,
    COALESCE(t.team_name, 'UNASSIGNED') AS responsible_team
FROM incident_component ic
JOIN component c ON ic.component_id = c.component_id
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE ic.incident_id = %s
ORDER BY FIELD(ic.impact_level,'RootCause','Unavailable','Degraded','Minor'),
         c.component_name
"""

DEPLOYMENTS_LIST = """
SELECT
    d.deployment_id,
    c.component_id,
    c.component_name,
    d.version,
    d.deployed_at,
    d.status,
    d.is_rollback,
    e.environment_name,
    e.is_production,
    dev.developer_name AS deployed_by
FROM deployment d
JOIN component c        ON d.component_id   = c.component_id
JOIN environment e      ON d.environment_id = e.environment_id
LEFT JOIN developer dev ON d.deployed_by    = dev.developer_id
WHERE (%s IS NULL OR e.environment_name = %s)
  AND (%s IS NULL OR d.status           = %s)
ORDER BY d.deployed_at DESC
LIMIT %s OFFSET %s
"""

# ---------------------------------------------------------------------
# Analytics - these read the views and functions directly
# ---------------------------------------------------------------------

ANALYTICS_SUMMARY = """
SELECT
    (SELECT COUNT(*) FROM application)                                   AS applications,
    (SELECT COUNT(*) FROM component)                                     AS components,
    (SELECT COUNT(*) FROM component WHERE is_active = 0)                 AS retired_components,
    (SELECT COUNT(*) FROM component WHERE owner_team_id IS NULL)         AS unowned_components,
    (SELECT COUNT(*) FROM component WHERE criticality = 'Critical')      AS critical_components,
    (SELECT COUNT(*) FROM team)                                          AS teams,
    (SELECT COUNT(*) FROM developer)                                     AS developers,
    (SELECT COUNT(*) FROM dependency)                                    AS dependencies,
    (SELECT COUNT(*) FROM environment)                                   AS environments,
    (SELECT COUNT(*) FROM deployment)                                    AS deployments,
    (SELECT COUNT(*) FROM incident)                                      AS incidents,
    (SELECT COUNT(*) FROM incident WHERE status <> 'Resolved')            AS open_incidents,
    (SELECT COUNT(*) FROM incident WHERE severity = 'SEV1')               AS sev1_incidents,
    (SELECT COUNT(*) FROM incident
      WHERE severity = 'SEV1' AND status <> 'Resolved')                  AS open_sev1_incidents,
    (SELECT COUNT(*) FROM incident
      WHERE caused_by_deployment_id IS NOT NULL)                         AS deploy_caused_incidents,
    (SELECT ROUND(AVG(TIMESTAMPDIFF(MINUTE, started_at, resolved_at)), 1)
       FROM incident WHERE resolved_at IS NOT NULL)                      AS avg_resolution_minutes,
    (SELECT COUNT(*) FROM production_infrastructure_view)                AS components_in_production,
    (SELECT COUNT(*) FROM deployment WHERE status = 'Failed')             AS failed_deployments,
    (SELECT COUNT(*) FROM deployment WHERE is_rollback = 1)               AS rollbacks
"""

ANALYTICS_HIGH_RISK = """
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED')       AS owning_team,
    fn_direct_dependent_count(c.component_id) AS direct_dependents,
    fn_blast_radius_count(c.component_id)     AS blast_radius,
    fn_incident_count(c.component_id)         AS incident_count,
    fn_risk_score(c.component_id)             AS risk_score,
    CASE
        WHEN fn_blast_radius_count(c.component_id) >= 5 THEN 'SEVERE'
        WHEN fn_blast_radius_count(c.component_id) >= 3 THEN 'HIGH'
        WHEN fn_blast_radius_count(c.component_id) >= 1 THEN 'MODERATE'
        ELSE 'ISOLATED'
    END                                       AS blast_radius_band
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
ORDER BY risk_score DESC, blast_radius DESC, c.component_name
LIMIT %s
"""

ANALYTICS_CRITICAL_INCIDENTS = """
SELECT
    v.incident_id, v.title, v.severity, v.incident_status,
    v.started_at, v.resolved_at, v.environment_name,
    v.component_name, v.impact_level, v.responsible_team, v.resolution_minutes
FROM incident_impact_view v
WHERE v.severity IN ('SEV1', 'SEV2')
ORDER BY v.started_at DESC,
         FIELD(v.impact_level,'RootCause','Unavailable','Degraded','Minor')
LIMIT %s
"""

ANALYTICS_TEAM_HEALTH = """
SELECT
    COALESCE(t.team_name, 'UNASSIGNED')            AS team_name,
    COUNT(c.component_id)                          AS components,
    SUM(fn_risk_score(c.component_id))             AS total_risk_score,
    ROUND(AVG(fn_risk_score(c.component_id)), 1)   AS avg_risk_score,
    MAX(fn_risk_score(c.component_id))             AS worst_component_score,
    SUM(fn_incident_count(c.component_id))         AS incident_involvements,
    SUM(CASE WHEN c.criticality = 'Critical' THEN 1 ELSE 0 END) AS critical_components,
    (SELECT c2.component_name FROM component c2
      WHERE c2.owner_team_id <=> c.owner_team_id AND c2.is_active = 1
      ORDER BY fn_risk_score(c2.component_id) DESC, c2.component_name
      LIMIT 1)                                     AS riskiest_component
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
GROUP BY c.owner_team_id, t.team_name
ORDER BY total_risk_score DESC
"""

ANALYTICS_PRODUCTION = """
SELECT
    component_id, component_name, component_type, criticality,
    owning_team, environment_name, region,
    running_version, deployed_at, deployed_by
FROM production_infrastructure_view
ORDER BY FIELD(criticality, 'Critical', 'High', 'Medium', 'Low'), component_name
"""

ANALYTICS_UNOWNED = """
SELECT
    c.component_id, c.component_name, c.component_type, c.criticality,
    c.is_active,
    fn_direct_dependent_count(c.component_id) AS direct_dependents,
    fn_blast_radius_count(c.component_id)     AS blast_radius,
    fn_incident_count(c.component_id)         AS incident_count
FROM component c
WHERE c.owner_team_id IS NULL
ORDER BY fn_blast_radius_count(c.component_id) DESC, c.component_name
"""

ANALYTICS_INCIDENT_TREND = """
SELECT
    month_start,
    incidents,
    sev1,
    SUM(incidents) OVER (ORDER BY month_start)             AS cumulative_incidents,
    LAG(incidents)  OVER (ORDER BY month_start)            AS previous_month,
    incidents - LAG(incidents) OVER (ORDER BY month_start) AS change_vs_previous,
    ROUND(AVG(incidents) OVER (ORDER BY month_start
                               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 1)
                                                           AS three_month_moving_avg,
    ROUND(avg_resolution, 1)                               AS avg_resolution_minutes
FROM (
    SELECT DATE_FORMAT(started_at, '%%Y-%%m-01')                   AS month_start,
           COUNT(*)                                              AS incidents,
           SUM(CASE WHEN severity = 'SEV1' THEN 1 ELSE 0 END)      AS sev1,
           AVG(TIMESTAMPDIFF(MINUTE, started_at, resolved_at))    AS avg_resolution
    FROM incident
    GROUP BY DATE_FORMAT(started_at, '%%Y-%%m-01')
) monthly
ORDER BY month_start
"""

ANALYTICS_DEPLOY_INCIDENT_CORRELATION = """
SELECT
    c.component_name,
    d.version          AS deployed_version,
    d.deployed_at,
    i.incident_id,
    i.severity,
    i.title,
    i.started_at       AS incident_started_at,
    ic.impact_level,
    TIMESTAMPDIFF(HOUR, d.deployed_at, i.started_at) AS hours_after_deploy,
    CASE
        WHEN i.caused_by_deployment_id = d.deployment_id THEN 'CONFIRMED'
        ELSE 'CORRELATION ONLY'
    END                AS causal_status
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN component   c         ON d.component_id   = c.component_id
JOIN incident_component ic ON ic.component_id  = c.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
WHERE e.is_production = 1
  AND d.status = 'Success'
  AND i.started_at >  d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY
ORDER BY (i.caused_by_deployment_id = d.deployment_id) DESC, i.started_at DESC
"""

# Whole graph, for the dependency visualisation.
GRAPH_NODES = """
SELECT
    c.component_id,
    c.component_name,
    c.component_type,
    c.criticality,
    c.is_active,
    COALESCE(t.team_name, 'UNASSIGNED')   AS owning_team,
    fn_blast_radius_count(c.component_id) AS blast_radius,
    fn_risk_score(c.component_id)         AS risk_score
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
ORDER BY c.component_id
"""

GRAPH_EDGES = """
SELECT
    dependency_id,
    component_id   AS source_id,
    component_name AS source_name,
    depends_on_id  AS target_id,
    depends_on_name AS target_name,
    dependency_type,
    is_critical
FROM dependency_edge_view
ORDER BY component_name, depends_on_name
"""

# The depth bound is the component count, not a constant. This walk uses
# UNION ALL to carry the path, so unlike the UNION traversals it does not
# self-terminate on cyclic data. A simple cycle visits at most N components.
CYCLE_CHECK = """
WITH RECURSIVE walk (origin_id, current_id, depth, path, closed) AS (
    SELECT c.component_id, c.component_id, 0,
           CAST(c.component_name AS CHAR(1000)), 0
    FROM component c
    UNION ALL
    SELECT w.origin_id, d.depends_on_id, w.depth + 1,
           CONCAT(w.path, ' -> ', c2.component_name),
           CASE WHEN d.depends_on_id = w.origin_id THEN 1 ELSE 0 END
    FROM walk w
    JOIN dependency d ON d.component_id = w.current_id
    JOIN component c2 ON d.depends_on_id = c2.component_id
    WHERE w.closed = 0 AND w.depth < (SELECT COUNT(*) FROM component)
)
SELECT c.component_name AS component_in_cycle, w.depth AS cycle_length, w.path AS cycle_path
FROM walk w JOIN component c ON w.origin_id = c.component_id
WHERE w.closed = 1
ORDER BY w.depth, c.component_name
"""
