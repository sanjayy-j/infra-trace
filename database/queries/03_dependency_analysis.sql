-- =====================================================================
-- InfraTrace - 03. Dependency analysis (single level)
-- File   : database/queries/03_dependency_analysis.sql
--
-- Direct, one-hop relationships in the dependency graph. Multi-level
-- traversal lives in 07_blast_radius.sql.
--
-- Reading direction reminder:
--     dependency.component_id  DEPENDS ON  dependency.depends_on_id
--
-- SQL demonstrated: SELF JOIN (the same table joined twice under different
-- aliases), INNER vs LEFT JOIN, GROUP BY, HAVING, NOT EXISTS, CASE.
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
-- D1. What does the Order Service depend on?
--     Everything it needs to function, and who owns each piece - the
--     contact list when Order Service starts failing.
--
--     This is a SELF JOIN: `component` appears twice, once as the
--     dependent (src) and once as the dependency (tgt). That is the
--     direct consequence of modelling the graph as a self-referencing
--     many-to-many relationship on one table.
-- ---------------------------------------------------------------------
SELECT
    src.component_name                  AS service,
    tgt.component_name                  AS depends_on,
    tgt.component_type,
    d.dependency_type,
    CASE WHEN d.is_critical = 1 THEN 'YES' ELSE 'no' END AS critical_path,
    COALESCE(t.team_name, 'UNASSIGNED') AS dependency_owner,
    d.description
FROM dependency d
JOIN component src ON d.component_id  = src.component_id
JOIN component tgt ON d.depends_on_id = tgt.component_id
LEFT JOIN team t   ON tgt.owner_team_id = t.team_id
WHERE src.component_name = 'Order Service'
ORDER BY d.is_critical DESC, tgt.component_name;


-- ---------------------------------------------------------------------
-- D2. Which components depend on the Redis Cache?
--     The reverse direction: "we need to restart the cache tonight - who
--     is affected, and whose approval do we need?"
-- ---------------------------------------------------------------------
SELECT
    tgt.component_name                  AS component_under_change,
    src.component_name                  AS dependent_component,
    src.component_type,
    src.criticality                     AS dependent_criticality,
    d.dependency_type,
    CASE WHEN d.is_critical = 1 THEN 'YES' ELSE 'no' END AS critical_path,
    COALESCE(t.team_name, 'UNASSIGNED') AS team_to_notify,
    COALESCE(t.team_email, 'nobody')    AS contact
FROM dependency d
JOIN component tgt ON d.depends_on_id = tgt.component_id
JOIN component src ON d.component_id  = src.component_id
LEFT JOIN team t   ON src.owner_team_id = t.team_id
WHERE tgt.component_name = 'Redis Cache'
ORDER BY d.is_critical DESC, src.component_name;


-- ---------------------------------------------------------------------
-- D3. Which components have the most direct dependents?
--     The shared building blocks. A component many things depend on
--     cannot be changed casually, whatever its own criticality says.
--     HAVING filters on the aggregate, which WHERE cannot do.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED') AS owning_team,
    COUNT(d.dependency_id)              AS direct_dependents,
    SUM(d.is_critical)                  AS dependents_on_critical_path,
    SUM(CASE WHEN d.dependency_type = 'Asynchronous' THEN 1 ELSE 0 END)
                                        AS async_dependents,
    GROUP_CONCAT(src.component_name ORDER BY src.component_name SEPARATOR ', ')
                                        AS depended_on_by
FROM component c
JOIN dependency d  ON c.component_id = d.depends_on_id
JOIN component src ON d.component_id = src.component_id
LEFT JOIN team t   ON c.owner_team_id = t.team_id
GROUP BY c.component_id, c.component_name, c.component_type,
         c.criticality, t.team_name
HAVING direct_dependents >= 2          -- ignore leaves
ORDER BY direct_dependents DESC, c.component_name;


-- ---------------------------------------------------------------------
-- D4. Which dependencies cross a team boundary?
--     Every cross-team edge is a coordination cost and an escalation
--     path: when it breaks, two teams have to talk. Edges where the
--     downstream side is unowned are the worst case, because there is
--     nobody on the other end.
-- ---------------------------------------------------------------------
SELECT
    COALESCE(t_src.team_name, 'UNASSIGNED') AS calling_team,
    COALESCE(t_tgt.team_name, 'UNASSIGNED') AS providing_team,
    COUNT(*)                                AS edges,
    SUM(d.is_critical)                      AS critical_edges,
    CASE
        WHEN tgt.owner_team_id IS NULL THEN 'DEPENDS ON UNOWNED INFRASTRUCTURE'
        WHEN src.owner_team_id IS NULL THEN 'unowned component calling out'
        ELSE 'cross-team, both owned'
    END                                     AS risk_note,
    GROUP_CONCAT(CONCAT(src.component_name, ' -> ', tgt.component_name)
                 ORDER BY src.component_name SEPARATOR '; ') AS edge_list
FROM dependency d
JOIN component src      ON d.component_id  = src.component_id
JOIN component tgt      ON d.depends_on_id = tgt.component_id
LEFT JOIN team t_src    ON src.owner_team_id = t_src.team_id
LEFT JOIN team t_tgt    ON tgt.owner_team_id = t_tgt.team_id
-- only edges where the two ends have DIFFERENT owners.
-- The NULL-safe operator <=> is needed because src = NULL and tgt = NULL
-- would compare as NULL (unknown) rather than as equal, and plain <>
-- would then wrongly treat two unowned components as a cross-team edge.
WHERE NOT (src.owner_team_id <=> tgt.owner_team_id)
GROUP BY t_src.team_name, t_tgt.team_name,
         src.owner_team_id, tgt.owner_team_id
ORDER BY critical_edges DESC, edges DESC;


-- ---------------------------------------------------------------------
-- D5. Where does the graph begin and end?
--     ROOTS   - nothing depends on them. Entry points: gateways, and the
--               user-facing surface of the system.
--     LEAVES  - they depend on nothing. Foundations: databases, caches,
--               third-party APIs. A leaf failure has no upstream cause
--               inside the system, so it is always a real outage.
--     ISOLATED- neither. Usually a mistake, or a retired component whose
--               edges were removed.
--
--     Classified with two NOT EXISTS tests rather than counting joins.
-- ---------------------------------------------------------------------
SELECT
    CASE
        WHEN NOT EXISTS (SELECT 1 FROM dependency d WHERE d.depends_on_id = c.component_id)
         AND NOT EXISTS (SELECT 1 FROM dependency d WHERE d.component_id  = c.component_id)
            THEN '3. ISOLATED (no edges at all)'
        WHEN NOT EXISTS (SELECT 1 FROM dependency d WHERE d.depends_on_id = c.component_id)
            THEN '1. ROOT (nothing depends on it)'
        WHEN NOT EXISTS (SELECT 1 FROM dependency d WHERE d.component_id  = c.component_id)
            THEN '2. LEAF (depends on nothing)'
        ELSE '4. INTERMEDIATE'
    END                                 AS graph_position,
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED') AS owning_team,
    fn_direct_dependent_count(c.component_id) AS dependents,
    (SELECT COUNT(*) FROM dependency d WHERE d.component_id = c.component_id) AS dependencies
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
ORDER BY graph_position, c.component_name;
