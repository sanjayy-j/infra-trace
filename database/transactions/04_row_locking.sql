-- =====================================================================
-- InfraTrace - Transactions 4: row-level locking
-- File   : database/transactions/04_row_locking.sql
-- Sessions required: TWO
--
-- Run the numbered blocks in order across two clients.
--
-- Prerequisite: transactions/00_demo_setup.sql
-- =====================================================================

-- #####################################################################
-- DEMO A - InnoDB locks ROWS, not the whole table
--
-- Two engineers deploying two DIFFERENT components must not block each
-- other. Two engineers deploying the SAME component must.
-- #####################################################################

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
SELECT component_id, component_name, criticality
  FROM component WHERE component_id = 9001 FOR UPDATE;
-- A now holds an exclusive lock on the row for component 9001 ONLY.

-- STEP 2  ** SESSION B **
-- A DIFFERENT row: returns immediately, no waiting.
START TRANSACTION;
SELECT component_id, component_name, criticality
  FROM component WHERE component_id = 9002 FOR UPDATE;
-- expect: returns at once. Row-level locking, not table-level.
COMMIT;

-- STEP 3  ** SESSION B **
-- The SAME row A holds: this BLOCKS.
START TRANSACTION;
SELECT component_id, component_name FROM component WHERE component_id = 9001 FOR UPDATE;
-- ** BLOCKS ** until A commits or rolls back.

-- STEP 4  ** SESSION A **
COMMIT;
-- B's statement in step 3 now completes.

-- STEP 5  ** SESSION B **
COMMIT;


-- #####################################################################
-- DEMO B - a plain SELECT is never blocked by a write
--
-- This is the practical value of MVCC: InfraTrace's read-only analysis
-- (blast radius, dashboards, reports) keeps working at full speed while
-- engineers are writing. Readers do not block writers, and writers do not
-- block readers.
-- #####################################################################

-- STEP 1  ** SESSION A **
START TRANSACTION;
UPDATE component SET criticality = 'Critical' WHERE component_id = 9001;
-- uncommitted; A holds an exclusive row lock

-- STEP 2  ** SESSION B **
-- A plain, non-locking SELECT. Returns INSTANTLY from the MVCC snapshot.
SELECT criticality AS b_sees FROM component WHERE component_id = 9001;
-- expect: 'High' - the last COMMITTED value, returned without waiting.

-- The whole blast-radius analysis also runs unblocked:
SELECT fn_blast_radius_count(9001) AS blast_radius_still_queryable;

-- STEP 3  ** SESSION B **
-- But a LOCKING read does wait, because it needs the real current row.
SELECT criticality FROM component WHERE component_id = 9001 FOR SHARE;
-- ** BLOCKS ** until A finishes.

-- STEP 4  ** SESSION A **
ROLLBACK;
-- B's step 3 completes and reports 'High'.


-- #####################################################################
-- DEMO C - lock wait timeout
--
-- A blocked statement does not wait forever. After
-- innodb_lock_wait_timeout seconds it gives up with error 1205.
-- #####################################################################

SELECT @@innodb_lock_wait_timeout AS lock_wait_timeout_seconds;

-- STEP 1  ** SESSION B **   shorten the timeout so the demo is quick
SET SESSION innodb_lock_wait_timeout = 3;

-- STEP 2  ** SESSION A **
START TRANSACTION;
SELECT * FROM component WHERE component_id = 9001 FOR UPDATE;
-- hold the lock and do nothing

-- STEP 3  ** SESSION B **
START TRANSACTION;
SELECT * FROM component WHERE component_id = 9001 FOR UPDATE;
-- After ~3 seconds:
--   ERROR 1205 (HY000): Lock wait timeout exceeded; try restarting transaction
--
-- Note the advice in the message. A timeout is a TRANSIENT failure - the
-- right response in application code is to roll back and retry, not to
-- report a permanent error to the user.
ROLLBACK;

-- STEP 4  ** SESSION A **
COMMIT;


-- #####################################################################
-- DEMO D - NOWAIT and SKIP LOCKED  (MySQL 8.0+)
--
-- Sometimes waiting is the wrong behaviour and you want to decide what to
-- do instead. MySQL 8 gives two options on a locking read.
-- #####################################################################

-- STEP 1  ** SESSION A **
START TRANSACTION;
SELECT component_id FROM component WHERE component_id = 9001 FOR UPDATE;

-- STEP 2  ** SESSION B **   NOWAIT: fail immediately rather than block
START TRANSACTION;
SELECT component_id FROM component WHERE component_id = 9001 FOR UPDATE NOWAIT;
--   ERROR 3572 (HY000): Statement aborted because lock(s) could not be acquired
--                       immediately and NOWAIT is set.
-- Useful for an interactive UI: tell the user "someone else is editing
-- this component" straight away instead of freezing for 50 seconds.
ROLLBACK;

-- STEP 3  ** SESSION B **   SKIP LOCKED: quietly ignore locked rows
START TRANSACTION;
SELECT component_id, component_name
  FROM component
 WHERE component_id IN (9001, 9002)
   FOR UPDATE SKIP LOCKED;
-- expect: ONLY 9002. Row 9001 is locked by A, so it is skipped rather
-- than waited for. This is the standard way to build a work queue that
-- several workers can drain in parallel without contending.
COMMIT;

-- STEP 4  ** SESSION A **
COMMIT;


-- #####################################################################
-- INSPECTING LOCKS - what to run when something is stuck
-- #####################################################################

-- Who is waiting for whom, and on which statement (MySQL 8):
SELECT
    waiting_trx_id, waiting_pid, waiting_query,
    blocking_trx_id, blocking_pid, blocking_query
FROM sys.innodb_lock_waits;

-- Every open transaction and how long it has been running:
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS seconds_open,
       trx_rows_locked, trx_query
FROM information_schema.innodb_trx
ORDER BY trx_started;

-- The individual locks currently held or requested:
SELECT engine_transaction_id, object_name, index_name,
       lock_type, lock_mode, lock_status, lock_data
FROM performance_schema.data_locks
ORDER BY engine_transaction_id;

-- Reset
UPDATE component SET criticality = 'High' WHERE component_id = 9001;
