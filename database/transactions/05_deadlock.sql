-- =====================================================================
-- InfraTrace - Transactions 5: deadlock
-- File   : database/transactions/05_deadlock.sql
-- Sessions required: TWO
--
-- Run the numbered blocks in order across two clients.
--
-- Prerequisite: transactions/00_demo_setup.sql
-- =====================================================================

-- #####################################################################
-- WHAT A DEADLOCK IS
--
-- A deadlock is not a slow query and not a timeout. It is a CYCLE in the
-- waits-for graph: A holds a lock B wants, and B holds a lock A wants.
-- Neither can ever proceed, so waiting longer cannot help.
--
-- InnoDB detects the cycle immediately and breaks it by killing one
-- transaction - the "victim" - with error 1213. The other proceeds.
--
-- The pleasing detail for this project: InnoDB is doing exactly the same
-- job as sp_detect_dependency_cycles(). Both find a cycle in a directed
-- graph. One walks "transaction waits for transaction", the other walks
-- "component depends on component".
-- #####################################################################


-- =====================================================================
-- DEMO 1 - produce a real deadlock
--
-- Scenario: two engineers are each reassigning a PAIR of components to a
-- different owning team, and they happen to process the pair in OPPOSITE
-- orders.
--
--   Session A:  lock 9001, then 9002
--   Session B:  lock 9002, then 9001
--
-- That opposite ordering is the entire cause.
-- =====================================================================

-- STEP 1  ** SESSION A **
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

START TRANSACTION;
UPDATE component SET criticality = 'Critical' WHERE component_id = 9001;
-- A now holds the lock on 9001.

-- STEP 2  ** SESSION B **
START TRANSACTION;
UPDATE component SET criticality = 'Critical' WHERE component_id = 9002;
-- B now holds the lock on 9002. No conflict yet.

-- STEP 3  ** SESSION A **
UPDATE component SET criticality = 'Low' WHERE component_id = 9002;
-- ** A BLOCKS ** waiting for B's lock on 9002.

-- STEP 4  ** SESSION B **
UPDATE component SET criticality = 'Low' WHERE component_id = 9001;
-- The cycle closes. InnoDB detects it instantly and kills ONE session:
--
--   ERROR 1213 (40001): Deadlock found when trying to get lock;
--                       try restarting transaction
--
-- Whichever session receives 1213 has been rolled back ENTIRELY - not
-- just the one statement. The survivor continues normally.

-- STEP 5  ** BOTH SESSIONS **
-- The victim must start over; the survivor should commit or roll back.
ROLLBACK;

-- STEP 6  ** EITHER SESSION **   inspect the post-mortem
SHOW ENGINE INNODB STATUS;
-- Look for the "LATEST DETECTED DEADLOCK" section. It names both
-- transactions, the exact statement each was running, the locks each held
-- and wanted, and which one was rolled back. This is the single most
-- useful output for diagnosing a production deadlock.

-- Cumulative deadlock counter for this server.
-- Note: there is no SHOW GLOBAL STATUS LIKE 'innodb_deadlocks' in MySQL 8
-- (that variable exists only in MariaDB/Percona). The portable place to
-- read it in MySQL 8 is innodb_metrics.
SELECT name, count
FROM information_schema.innodb_metrics
WHERE name IN ('lock_deadlocks', 'lock_deadlock_false_positives');
-- lock_deadlocks increments by 1 each time InnoDB breaks a wait cycle.

-- Reset
UPDATE component SET criticality = 'High' WHERE component_id IN (9001, 9002);


-- =====================================================================
-- DEMO 2 - the fix: a consistent lock ordering
--
-- Deadlocks between two transactions disappear if every transaction
-- acquires locks in the SAME order. Ordering by primary key is the
-- simplest rule that is easy to follow everywhere.
--
-- Now BOTH sessions go 9001 then 9002. Run the same interleaving as
-- demo 1 and no deadlock is possible: the second session simply waits at
-- its first statement, then proceeds.
-- =====================================================================

-- STEP 1  ** SESSION A **
START TRANSACTION;
UPDATE component SET criticality = 'Critical' WHERE component_id = 9001;   -- lowest id first

-- STEP 2  ** SESSION B **
START TRANSACTION;
UPDATE component SET criticality = 'Critical' WHERE component_id = 9001;   -- same order
-- ** B BLOCKS here ** - waiting, but NOT deadlocked. B holds no lock that
-- A wants, so there is no cycle and the wait is guaranteed to end.

-- STEP 3  ** SESSION A **
UPDATE component SET criticality = 'Low' WHERE component_id = 9002;
COMMIT;
-- B's step 2 now proceeds.

-- STEP 4  ** SESSION B **
UPDATE component SET criticality = 'Low' WHERE component_id = 9002;
COMMIT;
-- Both transactions completed. No 1213.

-- Reset
UPDATE component SET criticality = 'High' WHERE component_id IN (9001, 9002);


-- =====================================================================
-- DEMO 3 - handling a deadlock in application code: retry
--
-- Deadlock is a TRANSIENT failure. The correct response is to retry the
-- whole transaction, not to surface an error. This procedure shows the
-- pattern: catch SQLSTATE '40001', roll back, and try again up to a limit.
--
-- Run it in one session; it succeeds immediately with no contention. Its
-- value is the shape of the code, which is what real InfraTrace write
-- paths (the API's deployment recorder, for instance) should follow.
-- =====================================================================

USE infratrace;

DROP PROCEDURE IF EXISTS sp_demo_retry_on_deadlock;

DELIMITER $$
CREATE PROCEDURE sp_demo_retry_on_deadlock(IN p_component_id INT,
                                           IN p_new_criticality VARCHAR(10))
BEGIN
    DECLARE v_attempt   INT DEFAULT 0;
    DECLARE v_max_tries INT DEFAULT 3;
    DECLARE v_done      TINYINT DEFAULT 0;
    DECLARE v_deadlock  TINYINT DEFAULT 0;

    -- 40001 is the SQLSTATE for both deadlock (1213) and lock wait
    -- timeout (1205) - the two retryable concurrency failures.
    DECLARE CONTINUE HANDLER FOR SQLSTATE '40001' SET v_deadlock = 1;

    retry_loop: WHILE v_done = 0 AND v_attempt < v_max_tries DO
        SET v_attempt  = v_attempt + 1;
        SET v_deadlock = 0;

        START TRANSACTION;
        UPDATE component
           SET criticality = p_new_criticality
         WHERE component_id = p_component_id;

        IF v_deadlock = 1 THEN
            ROLLBACK;
            -- a real system would back off briefly before retrying
            DO SLEEP(0.1);
        ELSE
            COMMIT;
            SET v_done = 1;
        END IF;
    END WHILE;

    SELECT
        v_attempt AS attempts_used,
        IF(v_done = 1, 'committed', 'gave up after max retries') AS outcome;
END$$
DELIMITER ;

CALL sp_demo_retry_on_deadlock(9001, 'Critical');
SELECT criticality AS result FROM component WHERE component_id = 9001;

-- Reset
UPDATE component SET criticality = 'High' WHERE component_id = 9001;
DROP PROCEDURE sp_demo_retry_on_deadlock;


-- =====================================================================
-- SUMMARY
--
--   Lock wait timeout (1205)  one transaction waited too long. Only the
--                             STATEMENT failed. Retryable.
--   Deadlock (1213)           a genuine wait cycle. The whole
--                             TRANSACTION was rolled back. Retryable.
--
-- How to reduce deadlocks, in order of effectiveness:
--   1. Acquire locks in a consistent order everywhere (demo 2).
--   2. Keep transactions short - never hold locks across user think time
--      or a network round trip.
--   3. Touch the fewest rows possible; make sure UPDATE and DELETE are
--      driven by an index, because a full scan locks far more rows than
--      intended. This is where the indexes in database/performance/
--      pay off a second time.
--   4. Always implement retry (demo 3). Deadlocks cannot be eliminated
--      entirely in a concurrent system, so handling them is mandatory.
-- =====================================================================
