-- =====================================================================
-- InfraTrace - 02. Ownership analysis
-- File   : database/queries/02_ownership_analysis.sql
--
-- "Who is responsible for this?" - the question that has to be answerable
-- in seconds at 3am, and the one most often unanswerable in practice.
--
-- SQL demonstrated: LEFT JOIN (to find absence), GROUP_CONCAT, HAVING,
-- NOT EXISTS, CASE, UNION ALL.
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
-- O1. What does each team own?
--     The starting point for any on-call rotation.
--     LEFT JOIN from team, so a team owning nothing is still listed -
--     which is itself worth seeing.
-- ---------------------------------------------------------------------
SELECT
    t.team_name,
    COUNT(c.component_id)                                        AS components_owned,
    SUM(CASE WHEN c.criticality = 'Critical' THEN 1 ELSE 0 END)  AS critical_components,
    GROUP_CONCAT(c.component_name ORDER BY c.component_name SEPARATOR ', ') AS component_list
FROM team t
LEFT JOIN component c ON t.team_id = c.owner_team_id
GROUP BY t.team_id, t.team_name
ORDER BY components_owned DESC, t.team_name;


-- ---------------------------------------------------------------------
-- O2. Which components have NO owning team?
--     The operational risk this project exists to surface: when an
--     unowned component fails, nobody is paged. Sorted by blast radius,
--     so the most urgent gap is first.
--
--     This is why component.owner_team_id is deliberately nullable. A
--     NOT NULL column would have forced a fake owner and hidden exactly
--     the problem worth finding.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.component_type,
    c.criticality,
    CASE WHEN c.is_active = 1 THEN 'active' ELSE 'retired' END AS state,
    fn_direct_dependent_count(c.component_id)                  AS direct_dependents,
    fn_blast_radius_count(c.component_id)                      AS total_affected_if_down,
    fn_incident_count(c.component_id)                          AS past_incidents,
    (SELECT GROUP_CONCAT(DISTINCT a.application_name ORDER BY a.application_name SEPARATOR ', ')
       FROM application_component ac
       JOIN application a ON ac.application_id = a.application_id
      WHERE ac.component_id = c.component_id)                  AS used_by_applications
FROM component c
WHERE c.owner_team_id IS NULL
ORDER BY total_affected_if_down DESC, c.component_name;


-- ---------------------------------------------------------------------
-- O3. Which components does each application rely on?
--     Resolves the application-to-component many-to-many relationship,
--     and flags applications that depend on unowned infrastructure.
-- ---------------------------------------------------------------------
SELECT
    a.application_name,
    a.app_type,
    COALESCE(app_team.team_name, 'UNASSIGNED')                  AS application_owner,
    COUNT(ac.component_id)                                      AS components_used,
    SUM(CASE WHEN c.criticality = 'Critical' THEN 1 ELSE 0 END) AS critical_components,
    SUM(CASE WHEN c.owner_team_id IS NULL THEN 1 ELSE 0 END)    AS unowned_components,
    COUNT(DISTINCT c.owner_team_id)                             AS teams_involved,
    GROUP_CONCAT(c.component_name ORDER BY c.component_name SEPARATOR ', ') AS component_list
FROM application a
JOIN application_component ac ON a.application_id = ac.application_id
JOIN component c              ON ac.component_id  = c.component_id
LEFT JOIN team app_team       ON a.owner_team_id  = app_team.team_id
GROUP BY a.application_id, a.application_name, a.app_type, app_team.team_name
ORDER BY components_used DESC, a.application_name;


-- ---------------------------------------------------------------------
-- O4. Ownership and coverage gaps, in one report.
--     Four different kinds of "something is missing", collected with
--     UNION ALL. Each row is an item somebody should follow up.
--
--     NOT EXISTS is the right tool here rather than a LEFT JOIN with an
--     IS NULL test: it expresses "no matching row exists" directly, and
--     it can stop at the first match instead of building the whole join.
-- ---------------------------------------------------------------------
SELECT 'Component has no owning team' AS gap_type,
       c.component_name               AS item,
       c.criticality                  AS severity_hint,
       CONCAT(fn_blast_radius_count(c.component_id), ' components affected if it fails') AS detail
FROM component c
WHERE c.owner_team_id IS NULL

UNION ALL

SELECT 'Component belongs to no application',
       c.component_name,
       c.criticality,
       'Not linked to any application - is it still needed?'
FROM component c
WHERE NOT EXISTS (SELECT 1 FROM application_component ac
                  WHERE ac.component_id = c.component_id)
  AND c.is_active = 1

UNION ALL

SELECT 'Team owns no components',
       t.team_name,
       'n/a',
       'Team exists but owns nothing - stale record, or a gap in the model?'
FROM team t
WHERE NOT EXISTS (SELECT 1 FROM component c WHERE c.owner_team_id = t.team_id)

UNION ALL

SELECT 'Active component never reached production',
       c.component_name,
       c.criticality,
       CONCAT('Deployed only to: ', COALESCE(
           (SELECT GROUP_CONCAT(DISTINCT e.environment_name ORDER BY e.environment_name SEPARATOR ', ')
              FROM deployment d JOIN environment e ON d.environment_id = e.environment_id
             WHERE d.component_id = c.component_id), 'nowhere'))
FROM component c
WHERE c.is_active = 1
  AND NOT EXISTS (
      SELECT 1 FROM deployment d
      JOIN environment e ON d.environment_id = e.environment_id
      WHERE d.component_id = c.component_id AND e.is_production = 1)

ORDER BY gap_type, item;
