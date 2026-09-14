-- =====================================================================
-- InfraTrace - Stored Functions
-- File   : database/functions/functions.sql
-- Purpose: Reusable scalar calculations over the dependency graph, so the
--          same logic is never rewritten in a query, a procedure or the API.
--
-- A note on determinism: every function here is declared
-- NOT DETERMINISTIC + READS SQL DATA. That is the honest declaration -
-- these functions read tables, so the same argument can legitimately give
-- a different answer after the data changes. Declaring them DETERMINISTIC
-- would be wrong and is unsafe with statement-based binary logging.
--
-- DELIMITER is a client command understood by the mysql CLI and Workbench.
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

DROP FUNCTION IF EXISTS fn_direct_dependent_count;
DROP FUNCTION IF EXISTS fn_blast_radius_count;
DROP FUNCTION IF EXISTS fn_incident_count;
DROP FUNCTION IF EXISTS fn_would_create_cycle;
DROP FUNCTION IF EXISTS fn_dependency_depth;
DROP FUNCTION IF EXISTS fn_risk_score;

DELIMITER $$

-- ---------------------------------------------------------------------
-- fn_direct_dependent_count(p_component_id)
--   Purpose : how many components depend DIRECTLY on this one.
--             "If this breaks, who notices immediately?"
--   Returns : INT (0 if nothing depends on it)
--   Example : SELECT fn_direct_dependent_count(16);   -- Kafka -> 6
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_direct_dependent_count(p_component_id INT)
RETURNS INT
NOT DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_count INT;

    SELECT COUNT(*) INTO v_count
    FROM dependency
    WHERE depends_on_id = p_component_id;

    RETURN v_count;
END$$

-- ---------------------------------------------------------------------
-- fn_blast_radius_count(p_component_id)
--   Purpose : how many components are affected DIRECTLY OR INDIRECTLY if
--             this component fails, by walking the dependency graph
--             upwards. The component itself is not counted.
--   Returns : INT
--   Example : SELECT fn_blast_radius_count(11);   -- Payment DB -> 3
--
--   A recursive CTE is used, so traversal depth is not fixed. UNION (not
--   UNION ALL) removes duplicate paths, which also guarantees the
--   recursion terminates even if the data somehow contained a cycle.
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_blast_radius_count(p_component_id INT)
RETURNS INT
NOT DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_count INT;

    WITH RECURSIVE affected (component_id) AS (
        -- base: components that depend directly on the failing component
        SELECT d.component_id
        FROM dependency d
        WHERE d.depends_on_id = p_component_id

        UNION

        -- step: whoever depends on an already-affected component
        SELECT d.component_id
        FROM dependency d
        JOIN affected a ON d.depends_on_id = a.component_id
    )
    SELECT COUNT(DISTINCT component_id) INTO v_count
    FROM affected;

    RETURN v_count;
END$$

-- ---------------------------------------------------------------------
-- fn_incident_count(p_component_id)
--   Purpose : how many incidents this component has been involved in,
--             as root cause or as collateral damage.
--   Returns : INT
--   Example : SELECT fn_incident_count(3);   -- Order Service -> 5
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_incident_count(p_component_id INT)
RETURNS INT
NOT DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_count INT;

    SELECT COUNT(*) INTO v_count
    FROM incident_component
    WHERE component_id = p_component_id;

    RETURN v_count;
END$$

-- ---------------------------------------------------------------------
-- fn_would_create_cycle(p_component_id, p_depends_on_id)
--   Purpose : would recording "p_component_id depends on p_depends_on_id"
--             create a circular dependency of ANY length?
--   Returns : 1 if the edge would close a cycle, else 0
--   Example : SELECT fn_would_create_cycle(4, 3);  -- 1 (Order->Payment exists)
--             SELECT fn_would_create_cycle(1, 2);  -- 0 (safe)
--
--   How it works: an edge A -> B closes a cycle exactly when A is already
--   reachable FROM B by following depends_on edges. So the function walks
--   downwards from B and checks whether it ever arrives back at A.
--   This is the general test - it subsumes both the self-dependency case
--   and the direct mutual case A <-> B.
--
--   This function is what makes cycle PREVENTION possible in the dependency
--   triggers, rather than only after-the-fact detection.
--
--   ON THE DEPTH BOUND. The recursion uses UNION, which de-duplicates, so the
--   walk visits each component at most once and terminates after at most N
--   steps for N components - the cap is a safety net, not the termination
--   condition. It is set to 1000 to match MySQL's own default
--   cte_max_recursion_depth, which is the hard backstop that would abort the
--   query rather than let it run away.
--
--   So the honest claim is: cycles of ARBITRARY length are rejected, up to the
--   configured recursion limit. It is not literally unbounded. With the
--   default of 1000 this covers any graph of fewer than 1000 components; a
--   larger estate would need cte_max_recursion_depth raised to match.
--
--   An earlier version capped this at 50, which silently accepted a cycle
--   whose closing path was longer than 50 hops. That was a real defect, and
--   tests/sql/06_cycle_regression.sql now guards against its return.
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_would_create_cycle(p_component_id INT, p_depends_on_id INT)
RETURNS TINYINT
NOT DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_hit TINYINT DEFAULT 0;

    -- a component depending on itself is the degenerate 1-node cycle
    IF p_component_id = p_depends_on_id THEN
        RETURN 1;
    END IF;

    WITH RECURSIVE reach (component_id, depth) AS (
        SELECT d.depends_on_id, 1
        FROM dependency d
        WHERE d.component_id = p_depends_on_id

        UNION

        SELECT d.depends_on_id, r.depth + 1
        FROM dependency d
        JOIN reach r ON d.component_id = r.component_id
        WHERE r.depth < 1000      -- safety net only; see note below
    )
    SELECT EXISTS (SELECT 1 FROM reach WHERE component_id = p_component_id)
    INTO v_hit;

    RETURN v_hit;
END$$

-- ---------------------------------------------------------------------
-- fn_dependency_depth(p_component_id)
--   Purpose : how many levels deep this component's dependency chain
--             goes. A depth of 0 means it depends on nothing (a leaf,
--             typically a database or an external API).
--   Returns : INT
--   Example : SELECT fn_dependency_depth(1);   -- API Gateway -> 3
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_dependency_depth(p_component_id INT)
RETURNS INT
NOT DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_depth INT;

    WITH RECURSIVE needs (component_id, depth) AS (
        SELECT d.depends_on_id, 1
        FROM dependency d
        WHERE d.component_id = p_component_id

        UNION

        SELECT d.depends_on_id, n.depth + 1
        FROM dependency d
        JOIN needs n ON d.component_id = n.component_id
        WHERE n.depth < 1000      -- safety net only; see note below
    )
    SELECT COALESCE(MAX(depth), 0) INTO v_depth FROM needs;

    RETURN v_depth;
END$$

-- ---------------------------------------------------------------------
-- fn_risk_score(p_component_id)
--   Purpose : one number combining how far a failure spreads with how
--             critical the component is, so risk ranking is defined in
--             exactly ONE place and cannot drift between the SQL queries,
--             the stored procedures and the API.
--   Formula : blast_radius x criticality weight
--             (Critical 4, High 3, Medium 2, Low 1)
--   Returns : INT
--   Example : SELECT fn_risk_score(16);   -- Kafka: 7 x 4 = 28
--
--   This is a deliberately simple, explainable heuristic - not a
--   probability. It ranks components against each other; the absolute
--   number has no unit.
-- ---------------------------------------------------------------------
CREATE FUNCTION fn_risk_score(p_component_id INT)
RETURNS INT
NOT DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_weight INT DEFAULT 1;

    SELECT CASE criticality
               WHEN 'Critical' THEN 4
               WHEN 'High'     THEN 3
               WHEN 'Medium'   THEN 2
               ELSE 1
           END
    INTO v_weight
    FROM component
    WHERE component_id = p_component_id;

    RETURN COALESCE(fn_blast_radius_count(p_component_id) * v_weight, 0);
END$$

DELIMITER ;

SELECT 'Functions created: 6.' AS status;
