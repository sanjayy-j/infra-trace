-- =====================================================================
-- InfraTrace - Triggers
-- File   : database/triggers/triggers.sql
-- Purpose: Business rules that a CHECK constraint cannot express.
--
-- Two separate MySQL limitations put these rules in triggers:
--   1. A CHECK constraint may only inspect the row it is defined on.
--      The cycle rule, the retired-component rule and the incident
--      timestamp rule all need to read other rows or other tables.
--   2. A column used in a CHECK constraint may not also carry a foreign
--      key referential action (error 3823). That is why even the
--      one-row rule component_id <> depends_on_id lives here.
--
-- Every rule is enforced on INSERT *and* UPDATE, so no write path can
-- bypass it.
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

DROP TRIGGER IF EXISTS trg_dependency_validate_insert;
DROP TRIGGER IF EXISTS trg_dependency_validate_update;
DROP TRIGGER IF EXISTS trg_deployment_validate_insert;
DROP TRIGGER IF EXISTS trg_deployment_validate_update;
DROP TRIGGER IF EXISTS trg_deployment_block_inactive_prod;   -- superseded name
DROP TRIGGER IF EXISTS trg_incident_set_resolved_at;

DELIMITER $$

-- =====================================================================
-- 1. DEPENDENCY GRAPH VALIDATION  (full cycle PREVENTION)
--
--    fn_would_create_cycle() answers "would this edge close a loop of any
--    length?" by walking the graph, so these triggers prevent:
--       A -> A            (self-dependency)
--       A -> B -> A       (direct mutual)
--       A -> B -> C -> A  (transitive, any length up to the recursion limit)
--
--    This is genuine prevention at the database level, not detection
--    after the fact: the offending INSERT/UPDATE is rejected.
-- =====================================================================

CREATE TRIGGER trg_dependency_validate_insert
BEFORE INSERT ON dependency
FOR EACH ROW
BEGIN
    IF NEW.component_id = NEW.depends_on_id THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Invalid dependency: a component cannot depend on itself.';
    END IF;

    IF fn_would_create_cycle(NEW.component_id, NEW.depends_on_id) = 1 THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Circular dependency rejected: this edge would close a dependency cycle.';
    END IF;
END$$

-- Same rules on UPDATE, so an existing edge cannot be edited into a cycle.
--
-- Caveat, stated honestly: the check runs BEFORE the update is applied, so
-- the row's OLD values are still present in the table while the reachability
-- walk runs. Re-pointing an edge in place can therefore be refused even
-- when the post-update graph would have been acyclic. Re-pointing is rare -
-- the normal workflow is DELETE the old edge then INSERT the new one - and a
-- conservative refusal is the safe direction to err in.
CREATE TRIGGER trg_dependency_validate_update
BEFORE UPDATE ON dependency
FOR EACH ROW
BEGIN
    IF NEW.component_id = NEW.depends_on_id THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Invalid dependency: a component cannot depend on itself.';
    END IF;

    IF (NEW.component_id <> OLD.component_id OR NEW.depends_on_id <> OLD.depends_on_id)
       AND fn_would_create_cycle(NEW.component_id, NEW.depends_on_id) = 1 THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Circular dependency rejected: this edge would close a dependency cycle.';
    END IF;
END$$

-- =====================================================================
-- 2. DEPLOYMENT VALIDATION
--    A component marked retired (is_active = 0) must never appear in a
--    production environment. This reads component and environment, so it
--    cannot be a CHECK constraint.
-- =====================================================================

CREATE TRIGGER trg_deployment_validate_insert
BEFORE INSERT ON deployment
FOR EACH ROW
BEGIN
    DECLARE v_is_active TINYINT;
    DECLARE v_is_prod   TINYINT;

    SELECT is_active     INTO v_is_active FROM component   WHERE component_id   = NEW.component_id;
    SELECT is_production INTO v_is_prod   FROM environment WHERE environment_id = NEW.environment_id;

    IF v_is_prod = 1 AND v_is_active = 0 THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Deployment rejected: component is retired and cannot go to production.';
    END IF;
END$$

-- The UPDATE counterpart. Without it, an UPDATE could move an existing
-- staging deployment onto a retired component in production and quietly
-- bypass the rule the INSERT trigger enforces.
CREATE TRIGGER trg_deployment_validate_update
BEFORE UPDATE ON deployment
FOR EACH ROW
BEGIN
    DECLARE v_is_active TINYINT;
    DECLARE v_is_prod   TINYINT;

    SELECT is_active     INTO v_is_active FROM component   WHERE component_id   = NEW.component_id;
    SELECT is_production INTO v_is_prod   FROM environment WHERE environment_id = NEW.environment_id;

    IF v_is_prod = 1 AND v_is_active = 0 THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Update rejected: component is retired and cannot be in production.';
    END IF;
END$$

-- =====================================================================
-- 3. INCIDENT TIMELINE MAINTENANCE
--    Keeps status and resolved_at consistent without the caller having to
--    remember both:
--      - closing an incident stamps resolved_at
--      - reopening an incident clears the stale resolution timestamp
--    A BEFORE UPDATE trigger runs before constraints are evaluated, so
--    this also keeps chk_incident_resolved satisfied automatically.
-- =====================================================================

CREATE TRIGGER trg_incident_set_resolved_at
BEFORE UPDATE ON incident
FOR EACH ROW
BEGIN
    IF NEW.status = 'Resolved' AND NEW.resolved_at IS NULL THEN
        SET NEW.resolved_at = NOW();
    END IF;

    IF NEW.status <> 'Resolved' AND OLD.status = 'Resolved' THEN
        SET NEW.resolved_at = NULL;
    END IF;
END$$

DELIMITER ;

SELECT 'Triggers created: 5.' AS status;
