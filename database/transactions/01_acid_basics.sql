-- =====================================================================
-- InfraTrace - Transactions 1: COMMIT, ROLLBACK, SAVEPOINT, atomicity
-- File   : database/transactions/01_acid_basics.sql
-- Sessions required: ONE. This file is safe to run top to bottom.
--
-- Run:  cd database
--       mysql -u root -p --table infratrace < transactions/01_acid_basics.sql
--
-- Prerequisite: transactions/00_demo_setup.sql
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

-- InnoDB runs in autocommit mode by default: every single statement is its
-- own transaction. START TRANSACTION suspends that until COMMIT or ROLLBACK.
SELECT @@autocommit AS autocommit_is_on, @@transaction_isolation AS default_isolation;

-- =====================================================================
-- 1. ROLLBACK - undoing work
--
-- Scenario: an engineer starts downgrading the demo component's
-- criticality, then realises it is wrong and abandons the change.
-- =====================================================================

SELECT '--- 1. ROLLBACK ---' AS demo;

SELECT criticality AS before_txn FROM component WHERE component_id = 9001;

START TRANSACTION;
    UPDATE component SET criticality = 'Low' WHERE component_id = 9001;
    -- Inside the transaction, this session already sees its own change.
    SELECT criticality AS inside_txn_uncommitted FROM component WHERE component_id = 9001;
ROLLBACK;

-- After ROLLBACK the change is gone completely.
SELECT criticality AS after_rollback FROM component WHERE component_id = 9001;


-- =====================================================================
-- 2. COMMIT - making work permanent
-- =====================================================================

SELECT '--- 2. COMMIT ---' AS demo;

START TRANSACTION;
    UPDATE component SET criticality = 'Critical' WHERE component_id = 9001;
COMMIT;

SELECT criticality AS after_commit FROM component WHERE component_id = 9001;

-- put it back so the file is re-runnable
UPDATE component SET criticality = 'High' WHERE component_id = 9001;


-- =====================================================================
-- 3. ATOMICITY - all of it, or none of it
--
-- Scenario: promoting a release is really TWO writes that belong together:
--   (a) record the production deployment
--   (b) close the incident that the release fixes
-- If (b) fails, (a) must not survive. Otherwise the database would claim a
-- release went out to fix an incident that is still open.
--
-- Here (b) fails on purpose: 'Closed' is not an allowed incident status,
-- so chk_incident_status rejects it.
-- =====================================================================

SELECT '--- 3. ATOMICITY: a partial failure must undo the whole unit ---' AS demo;

SELECT
    (SELECT COUNT(*) FROM deployment WHERE component_id = 9001) AS deployments_before,
    (SELECT status   FROM incident   WHERE incident_id  = 9001) AS incident_before;

DROP PROCEDURE IF EXISTS sp_demo_atomic_release;
DELIMITER $$
CREATE PROCEDURE sp_demo_atomic_release()
BEGIN
    -- EXITing the handler after ROLLBACK is what makes the unit atomic.
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        SELECT 'second statement failed -> ROLLBACK -> neither write survives' AS outcome;
    END;

    START TRANSACTION;

        -- (a) succeeds
        INSERT INTO deployment (component_id, environment_id, version, deployed_by, deployed_at, status)
        VALUES (9001, 3, 'v1.1.0', 3, '2026-09-12 09:00:00', 'Success');

        -- (b) fails: 'Closed' violates chk_incident_status
        UPDATE incident SET status = 'Closed' WHERE incident_id = 9001;

    COMMIT;
    SELECT 'both writes committed' AS outcome;
END$$
DELIMITER ;

CALL sp_demo_atomic_release();

-- Proof: the deployment count is unchanged and the incident is still Open.
-- The successful first write was undone along with the failed second one.
SELECT
    (SELECT COUNT(*) FROM deployment WHERE component_id = 9001) AS deployments_after,
    (SELECT status   FROM incident   WHERE incident_id  = 9001) AS incident_after,
    CASE WHEN (SELECT COUNT(*) FROM deployment WHERE component_id = 9001) = 2
          AND (SELECT status FROM incident WHERE incident_id = 9001) = 'Open'
         THEN 'ATOMICITY HELD'
         ELSE 'ATOMICITY VIOLATED'
    END AS verdict;

DROP PROCEDURE sp_demo_atomic_release;


-- =====================================================================
-- 4. The same unit of work, succeeding
--
-- With a valid status the whole unit commits together, and the trigger
-- trg_incident_set_resolved_at fills in resolved_at as part of the
-- same transaction.
-- =====================================================================

SELECT '--- 4. The same unit of work, this time valid ---' AS demo;

START TRANSACTION;
    INSERT INTO deployment (component_id, environment_id, version, deployed_by, deployed_at, status)
    VALUES (9001, 3, 'v1.1.0', 3, '2026-09-12 09:00:00', 'Success');

    UPDATE incident SET status = 'Resolved' WHERE incident_id = 9001;
COMMIT;

SELECT
    (SELECT COUNT(*) FROM deployment WHERE component_id = 9001) AS deployments_now,
    (SELECT status      FROM incident WHERE incident_id = 9001) AS incident_status,
    (SELECT resolved_at FROM incident WHERE incident_id = 9001) AS resolved_at_set_by_trigger;

-- restore the demo state
DELETE FROM deployment WHERE component_id = 9001 AND version = 'v1.1.0';
UPDATE incident SET status = 'Open' WHERE incident_id = 9001;


-- =====================================================================
-- 5. SAVEPOINT - partial rollback inside one transaction
--
-- Scenario: an engineer is bulk-registering three new dependencies for the
-- demo component. The third one is invalid (it would create a cycle, and
-- the trigger rejects it). With a SAVEPOINT the first two can be kept
-- while only the bad step is discarded.
-- =====================================================================

SELECT '--- 5. SAVEPOINT ---' AS demo;

INSERT INTO component (component_id, component_name, component_type, owner_team_id, criticality, created_on)
VALUES (9003, 'DEMO Coordinator Cache', 'Cache', 2, 'Medium', '2026-01-10'),
       (9004, 'DEMO Coordinator Queue', 'Queue', 2, 'Medium', '2026-01-10');

SELECT COUNT(*) AS demo_edges_before FROM dependency WHERE component_id = 9001;

START TRANSACTION;

    INSERT INTO dependency (component_id, depends_on_id, dependency_type)
    VALUES (9001, 9003, 'Synchronous');
    SAVEPOINT after_cache;

    INSERT INTO dependency (component_id, depends_on_id, dependency_type)
    VALUES (9001, 9004, 'Asynchronous');
    SAVEPOINT after_queue;

    -- Roll back only to after_cache: the queue edge is discarded,
    -- the cache edge is kept, and the transaction is still open.
    ROLLBACK TO SAVEPOINT after_cache;

    SELECT COUNT(*) AS edges_inside_txn_after_partial_rollback
    FROM dependency WHERE component_id = 9001;

COMMIT;

-- 2 edges: the original 9001->9002 plus the cache edge. The queue edge is gone.
SELECT d.depends_on_id, c.component_name AS kept_dependency
FROM dependency d JOIN component c ON d.depends_on_id = c.component_id
WHERE d.component_id = 9001 ORDER BY d.depends_on_id;

-- clean up the extra demo components
DELETE FROM component WHERE component_id IN (9003, 9004);

SELECT 'ACID basics demo complete. Demo rows 9001/9002 left in place.' AS status;
