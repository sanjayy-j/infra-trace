-- =====================================================================
-- InfraTrace - Transactions 2: isolation levels
-- File   : database/transactions/02_isolation_levels.sql
-- Sessions required: TWO
--
-- This file is NOT meant to be run top to bottom. Open two MySQL clients
-- side by side and run the numbered blocks in order: step 1 in A, step 2
-- in B, step 3 in A, and so on. The step number is the execution order.
--
-- Opening two sessions
--   Local MySQL:  two terminals, each running   mysql -u root -p infratrace
--   Docker:       two terminals, each running
--                 docker exec -it <container> mysql -u root -p infratrace
--
-- Prerequisite: transactions/00_demo_setup.sql
-- Reset between demos:
--   UPDATE component SET criticality = 'High' WHERE component_id = 9001;
-- =====================================================================


-- #####################################################################
-- DEMO A - READ COMMITTED vs REPEATABLE READ
--
-- The question: inside one transaction, if another session commits a
-- change, should I see it?
--   READ COMMITTED  - yes. Each statement sees the latest committed data.
--   REPEATABLE READ - no.  Every read in the transaction sees the same
--                     snapshot, taken at the first read.
-- MySQL's default is REPEATABLE READ.
-- #####################################################################

-- ---------------------------------------------------------------------
-- PART 1: READ COMMITTED - the value changes under the reader
-- ---------------------------------------------------------------------

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

SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
START TRANSACTION;
SELECT criticality AS a_first_read FROM component WHERE component_id = 9001;
-- expect: High

-- STEP 2  ** SESSION B **
UPDATE component SET criticality = 'Critical' WHERE component_id = 9001;
-- autocommit is on, so this is committed immediately

-- STEP 3  ** SESSION A **   (same transaction as step 1)
SELECT criticality AS a_second_read FROM component WHERE component_id = 9001;
-- expect: Critical   <-- the value CHANGED inside one transaction.
--         This is a "non-repeatable read".
COMMIT;

-- STEP 4  ** SESSION B **   reset
UPDATE component SET criticality = 'High' WHERE component_id = 9001;


-- ---------------------------------------------------------------------
-- PART 2: REPEATABLE READ - the reader keeps a stable snapshot
-- ---------------------------------------------------------------------

-- STEP 5  ** SESSION A **
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;
SELECT criticality AS a_first_read FROM component WHERE component_id = 9001;
-- expect: High

-- STEP 6  ** SESSION B **
UPDATE component SET criticality = 'Critical' WHERE component_id = 9001;

-- STEP 7  ** SESSION A **   (same transaction as step 5)
SELECT criticality AS a_second_read FROM component WHERE component_id = 9001;
-- expect: High   <-- STILL High. Session A reads its original snapshot
--         even though B committed. This is repeatable read.
COMMIT;

-- STEP 8  ** SESSION A **   after COMMIT the snapshot is released
SELECT criticality AS a_after_commit FROM component WHERE component_id = 9001;
-- expect: Critical

-- STEP 9  ** SESSION B **   reset
UPDATE component SET criticality = 'High' WHERE component_id = 9001;


-- #####################################################################
-- DEMO B - READ UNCOMMITTED and the DIRTY READ
--
-- READ UNCOMMITTED lets a session read data another session has written
-- but NOT committed. If that writer rolls back, the reader acted on a
-- value that never existed. That is a dirty read.
-- #####################################################################

-- STEP 1  ** SESSION A **
START TRANSACTION;
UPDATE component SET criticality = 'Low' WHERE component_id = 9001;
-- deliberately NOT committed yet

-- STEP 2  ** SESSION B **
SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT criticality AS b_dirty_read FROM component WHERE component_id = 9001;
-- expect: Low   <-- B is reading A's UNCOMMITTED value

-- STEP 3  ** SESSION A **
ROLLBACK;
-- the value 'Low' never existed as far as the database is concerned

-- STEP 4  ** SESSION B **
SELECT criticality AS b_read_after_rollback FROM component WHERE component_id = 9001;
-- expect: High
-- B briefly saw 'Low', a value no committed state ever held. Any decision
-- B made from it was based on data that vanished.

-- STEP 5  ** SESSION B **   go back to a safe default
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;


-- #####################################################################
-- DEMO C - PHANTOM ROWS
--
-- A phantom is a ROW that appears (rather than a value that changes).
-- Under REPEATABLE READ, InnoDB's snapshot hides rows another session
-- inserts, so a repeated COUNT(*) stays stable.
-- #####################################################################

-- STEP 1  ** SESSION A **
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;
SELECT COUNT(*) AS a_first_count FROM component WHERE owner_team_id = 2;
-- note the number

-- STEP 2  ** SESSION B **
INSERT INTO component (component_id, component_name, component_type, owner_team_id, criticality, created_on)
VALUES (9005, 'DEMO Phantom Service', 'Service', 2, 'Low', '2026-01-10');

-- STEP 3  ** SESSION A **   (same transaction)
SELECT COUNT(*) AS a_second_count FROM component WHERE owner_team_id = 2;
-- expect: the SAME number as step 1. The new row is a phantom that A's
--         snapshot does not include.
COMMIT;

-- STEP 4  ** SESSION A **   after commit the row becomes visible
SELECT COUNT(*) AS a_count_after_commit FROM component WHERE owner_team_id = 2;

-- STEP 5  ** SESSION B **   reset
DELETE FROM component WHERE component_id = 9005;


-- =====================================================================
-- SUMMARY - what each isolation level prevents
--
--  Isolation level    Dirty read   Non-repeatable read   Phantom
--  ---------------    ----------   -------------------   -------
--  READ UNCOMMITTED   possible     possible              possible
--  READ COMMITTED     prevented    possible              possible
--  REPEATABLE READ    prevented    prevented             prevented *
--  SERIALIZABLE       prevented    prevented             prevented
--
--  * InnoDB's REPEATABLE READ prevents phantoms for plain SELECTs via its
--    MVCC snapshot, and for locking reads via next-key (gap) locking.
--    The SQL standard only guarantees this at SERIALIZABLE, so this is
--    InnoDB being stricter than required.
--
-- Why InnoDB defaults to REPEATABLE READ: an InfraTrace blast-radius
-- traversal issues several recursive reads. If another session committed a
-- new dependency edge halfway through, a weaker level would let the
-- traversal mix old and new graph states and report an impact set that
-- never actually existed. A stable snapshot makes the answer coherent.
-- =====================================================================
