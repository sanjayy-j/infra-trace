-- =====================================================================
-- InfraTrace - Stored Procedures
-- File   : database/procedures/procedures.sql
-- Purpose: Packaged answers to the questions asked during an incident or
--          a change review. Each one is documented with its purpose,
--          parameters, result and an example invocation.
--
-- IMPORTANT: this file must be loaded AFTER functions.sql, because
-- sp_team_health_report and sp_detect_dependency_cycles call the
-- fn_* functions.
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

DROP PROCEDURE IF EXISTS sp_get_dependencies;
DROP PROCEDURE IF EXISTS sp_get_blast_radius;
DROP PROCEDURE IF EXISTS sp_team_health_report;
DROP PROCEDURE IF EXISTS sp_component_impact_report;
DROP PROCEDURE IF EXISTS sp_detect_dependency_cycles;

DELIMITER $$

-- ---------------------------------------------------------------------
-- sp_get_dependencies(p_component_id)
--   Purpose : the full downstream chain - everything this component needs
--             in order to work, directly or transitively.
--   Params  : p_component_id INT - the component to analyse
--   Returns : one row per required component, with the shallowest depth at
--             which it is reached, its type, criticality and owning team
--   Example : CALL sp_get_dependencies(3);   -- Order Service
-- ---------------------------------------------------------------------
CREATE PROCEDURE sp_get_dependencies(IN p_component_id INT)
BEGIN
    WITH RECURSIVE dep_chain (component_id, depth) AS (
        -- level 1: direct dependencies
        SELECT d.depends_on_id, 1
        FROM dependency d
        WHERE d.component_id = p_component_id

        UNION

        -- deeper levels: the dependencies of those dependencies
        SELECT d.depends_on_id, dc.depth + 1
        FROM dependency d
        JOIN dep_chain dc ON d.component_id = dc.component_id
        WHERE dc.depth < 1000      -- safety net; UNION already guarantees termination
    )
    SELECT
        MIN(dc.depth)                       AS depth,
        c.component_name                    AS depends_on,
        c.component_type,
        c.criticality,
        COALESCE(t.team_name, 'UNASSIGNED') AS owning_team
    FROM dep_chain dc
    JOIN component c ON dc.component_id = c.component_id
    LEFT JOIN team t ON c.owner_team_id = t.team_id
    GROUP BY c.component_id, c.component_name, c.component_type,
             c.criticality, t.team_name
    ORDER BY depth, depends_on;
END$$

-- ---------------------------------------------------------------------
-- sp_get_blast_radius(p_component_id)
--   Purpose : the full upstream chain - everything potentially affected if
--             this component fails, and which team would be paged.
--   Params  : p_component_id INT - the component assumed to have failed
--   Returns : one row per affected component, ordered by hops away
--   Example : CALL sp_get_blast_radius(11);  -- Payment DB
--
--   This is dependency-based POTENTIAL impact. See the note at the top of
--   queries/07_blast_radius.sql for what it does and does not model.
-- ---------------------------------------------------------------------
CREATE PROCEDURE sp_get_blast_radius(IN p_component_id INT)
BEGIN
    WITH RECURSIVE affected (component_id, depth) AS (
        SELECT d.component_id, 1
        FROM dependency d
        WHERE d.depends_on_id = p_component_id

        UNION

        SELECT d.component_id, a.depth + 1
        FROM dependency d
        JOIN affected a ON d.depends_on_id = a.component_id
        WHERE a.depth < 1000      -- safety net; UNION already guarantees termination
    )
    SELECT
        MIN(a.depth)                        AS hops_away,
        c.component_name                    AS affected_component,
        c.component_type,
        c.criticality,
        COALESCE(t.team_name, 'UNASSIGNED') AS team_to_notify,
        COALESCE(t.team_email, 'none')      AS contact
    FROM affected a
    JOIN component c ON a.component_id = c.component_id
    LEFT JOIN team t ON c.owner_team_id = t.team_id
    GROUP BY c.component_id, c.component_name, c.component_type,
             c.criticality, t.team_name, t.team_email
    ORDER BY hops_away, affected_component;
END$$

-- ---------------------------------------------------------------------
-- sp_team_health_report(p_team_id)
--   Purpose : one-screen summary for a single team - what it owns, how
--             exposed each component is, how deep its dependencies run,
--             and how many incidents it has seen.
--   Params  : p_team_id INT
--   Returns : one row per component owned by the team, worst risk first
--   Example : CALL sp_team_health_report(2);   -- Payments Team
-- ---------------------------------------------------------------------
CREATE PROCEDURE sp_team_health_report(IN p_team_id INT)
BEGIN
    SELECT
        t.team_name,
        c.component_name,
        c.component_type,
        c.criticality,
        fn_direct_dependent_count(c.component_id) AS direct_dependents,
        fn_blast_radius_count(c.component_id)     AS blast_radius,
        fn_dependency_depth(c.component_id)       AS dependency_depth,
        fn_incident_count(c.component_id)         AS incidents_recorded,
        fn_risk_score(c.component_id)             AS risk_score
    FROM component c
    JOIN team t ON c.owner_team_id = t.team_id
    WHERE t.team_id = p_team_id
    ORDER BY risk_score DESC, c.component_name;
END$$

-- ---------------------------------------------------------------------
-- sp_component_impact_report(p_component_id)
--   Purpose : the complete picture for one component in a single call -
--             what an engineer wants on screen when a page fires.
--   Params  : p_component_id INT
--   Returns : FIVE result sets, in this order:
--               1. the component itself, with its risk metrics
--               2. what it depends on (direct)
--               3. what depends on it (full blast radius, with hops)
--               4. applications reached through the blast radius
--               5. its incident history
--   Example : CALL sp_component_impact_report(11);   -- Payment DB
-- ---------------------------------------------------------------------
CREATE PROCEDURE sp_component_impact_report(IN p_component_id INT)
BEGIN
    -- 1. the component itself
    SELECT
        'COMPONENT' AS section,
        c.component_id,
        c.component_name,
        c.component_type,
        c.criticality,
        CASE WHEN c.is_active = 1 THEN 'active' ELSE 'retired' END AS state,
        COALESCE(t.team_name, 'UNASSIGNED') AS owning_team,
        fn_direct_dependent_count(c.component_id) AS direct_dependents,
        fn_blast_radius_count(c.component_id)     AS blast_radius,
        fn_dependency_depth(c.component_id)       AS dependency_depth,
        fn_risk_score(c.component_id)             AS risk_score
    FROM component c
    LEFT JOIN team t ON c.owner_team_id = t.team_id
    WHERE c.component_id = p_component_id;

    -- 2. direct dependencies
    SELECT
        'DEPENDS ON' AS section,
        tgt.component_name AS component,
        tgt.component_type,
        d.dependency_type,
        d.is_critical,
        COALESCE(t.team_name, 'UNASSIGNED') AS owning_team
    FROM dependency d
    JOIN component tgt ON d.depends_on_id = tgt.component_id
    LEFT JOIN team t   ON tgt.owner_team_id = t.team_id
    WHERE d.component_id = p_component_id
    ORDER BY d.is_critical DESC, tgt.component_name;

    -- 3. blast radius
    --
    -- Deliberately inlined rather than "CALL sp_get_blast_radius(...)".
    -- A nested CALL does not reliably surface its result set as a separate
    -- set to the client driver, so a caller walking result sets in order
    -- silently received the NEXT section's rows in this position. Repeating
    -- the traversal here costs a few lines and makes the procedure's
    -- five-result-set contract actually hold.
    WITH RECURSIVE affected (component_id, depth) AS (
        SELECT d.component_id, 1
        FROM dependency d
        WHERE d.depends_on_id = p_component_id

        UNION

        SELECT d.component_id, a.depth + 1
        FROM dependency d
        JOIN affected a ON d.depends_on_id = a.component_id
        WHERE a.depth < 1000      -- safety net; UNION already guarantees termination
    )
    SELECT
        'BLAST RADIUS'                      AS section,
        MIN(a.depth)                        AS hops_away,
        c.component_name                    AS affected_component,
        c.component_type,
        c.criticality,
        COALESCE(t.team_name, 'UNASSIGNED') AS team_to_notify,
        COALESCE(t.team_email, 'none')      AS contact
    FROM affected a
    JOIN component c ON a.component_id = c.component_id
    LEFT JOIN team t ON c.owner_team_id = t.team_id
    GROUP BY c.component_id, c.component_name, c.component_type,
             c.criticality, t.team_name, t.team_email
    ORDER BY hops_away, affected_component;

    -- 4. applications reached through the blast radius
    WITH RECURSIVE affected (component_id) AS (
        SELECT p_component_id
        UNION
        SELECT d.component_id
        FROM dependency d
        JOIN affected a ON d.depends_on_id = a.component_id
    )
    SELECT
        'AFFECTED APPLICATION' AS section,
        a.application_name,
        a.app_type,
        COUNT(DISTINCT c.component_id) AS affected_components_used,
        GROUP_CONCAT(DISTINCT c.component_name ORDER BY c.component_name SEPARATOR ', ')
                                       AS via_components
    FROM affected af
    JOIN component c              ON af.component_id = c.component_id
    JOIN application_component ac ON c.component_id  = ac.component_id
    JOIN application a            ON ac.application_id = a.application_id
    GROUP BY a.application_id, a.application_name, a.app_type
    ORDER BY affected_components_used DESC, a.application_name;

    -- 5. incident history
    SELECT
        'INCIDENT HISTORY' AS section,
        i.incident_id,
        i.severity,
        i.status,
        i.title,
        ic.impact_level,
        i.started_at,
        i.resolved_at
    FROM incident_component ic
    JOIN incident i ON ic.incident_id = i.incident_id
    WHERE ic.component_id = p_component_id
    ORDER BY i.started_at DESC;
END$$

-- ---------------------------------------------------------------------
-- sp_detect_dependency_cycles()
--   Purpose : find any circular dependency that already exists in the
--             data. The triggers PREVENT cycles on every INSERT and
--             UPDATE, so on a healthy database this returns no rows.
--
--             It still matters, because triggers can be bypassed:
--             a bulk load, a restored dump, a disabled trigger, or rows
--             inserted before the triggers were created. This procedure
--             is the audit that proves the graph is genuinely acyclic.
--
--   Params  : none
--   Returns : one row per component that participates in a cycle,
--             with the path that loops back to it. No rows = acyclic.
--   Example : CALL sp_detect_dependency_cycles();
-- ---------------------------------------------------------------------
CREATE PROCEDURE sp_detect_dependency_cycles()
BEGIN
    -- The bound below is derived, not guessed.
    --
    -- This walk uses UNION ALL rather than UNION, because the whole point is
    -- to report the PATH, and two different routes to the same component are
    -- two different findings. UNION ALL does not de-duplicate, so unlike the
    -- UNION-based traversals this recursion does NOT terminate on its own if
    -- the data contains a cycle that does not pass through the walk's origin -
    -- such a walk would circle forever. It therefore needs a real bound.
    --
    -- The exact bound is the number of components: a SIMPLE cycle can visit at
    -- most every component once, so any walk still open after N steps cannot
    -- be closing a cycle through its origin. Using N rather than an arbitrary
    -- constant makes the procedure correct for a graph of any size, and keeps
    -- it from running away on pathological data.
    --
    -- An earlier version hardcoded 20, which silently MISSED a real 60-node
    -- cycle. tests/sql/06_cycle_regression.sql now guards against that.
    DECLARE v_max_cycle_len INT;
    SELECT GREATEST(COUNT(*), 1) INTO v_max_cycle_len FROM component;

    WITH RECURSIVE walk (origin_id, current_id, depth, path, closed) AS (
        -- start one walk from every component
        SELECT
            c.component_id,
            c.component_id,
            0,
            CAST(c.component_name AS CHAR(1000)),
            0
        FROM component c

        UNION ALL

        SELECT
            w.origin_id,
            d.depends_on_id,
            w.depth + 1,
            CONCAT(w.path, ' -> ', c2.component_name),
            -- mark the step that arrives back at the component we started from
            CASE WHEN d.depends_on_id = w.origin_id THEN 1 ELSE 0 END
        FROM walk w
        JOIN dependency d ON d.component_id = w.current_id
        JOIN component c2 ON d.depends_on_id = c2.component_id
        -- stop expanding a walk once it has closed, and bound the rest
        WHERE w.closed = 0
          AND w.depth < v_max_cycle_len
    )
    SELECT
        c.component_name AS component_in_cycle,
        w.depth          AS cycle_length,
        w.path           AS cycle_path
    FROM walk w
    JOIN component c ON w.origin_id = c.component_id
    WHERE w.closed = 1
    ORDER BY w.depth, c.component_name;
END$$

DELIMITER ;

SELECT 'Procedures created: 5.' AS status;
