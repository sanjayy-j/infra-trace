-- =====================================================================
-- InfraTrace - Transactions 3: the lost update problem, and its cure
-- File   : database/transactions/03_lost_update.sql
-- Sessions required: TWO
--
-- Run the numbered blocks in order across two clients. The step number is
-- the execution order.
--
-- Prerequisite: transactions/00_demo_setup.sql
-- =====================================================================

-- #####################################################################
-- THE SCENARIO
--
-- Incident 9001 is open. Two responders are investigating in parallel and
-- each writes what they found into incident.root_cause:
--
--   Sneha  finds the connection pool is exhausted
--   Vikram finds a replica has slow disk I/O
--
-- Both read the field first (it is empty), both then write their own
-- finding. Neither transaction fails. No constraint is violated. And yet
-- one of the two findings is silently destroyed.
--
-- This is a LOST UPDATE: a read-modify-write cycle where two sessions
-- interleave, and the second write is based on a value that is already
-- stale by the time it lands.
-- #####################################################################


-- =====================================================================
-- DEMO 1 - the bug. Two sessions, no locking.
-- =====================================================================

-- Reset
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

UPDATE incident SET root_cause = NULL WHERE incident_id = 9001;

-- STEP 1  ** SESSION A (Sneha) **
START TRANSACTION;
SELECT root_cause AS sneha_reads FROM incident WHERE incident_id = 9001;
-- expect: NULL  -> "nothing recorded yet, I'll add my finding"

-- STEP 2  ** SESSION B (Vikram) **
START TRANSACTION;
SELECT root_cause AS vikram_reads FROM incident WHERE incident_id = 9001;
-- expect: NULL  -> Vikram reaches the SAME conclusion: the field is empty.
--                  This is the moment the bug becomes inevitable.

-- STEP 3  ** SESSION A (Sneha) **
UPDATE incident
   SET root_cause = 'Connection pool exhausted under load.'
 WHERE incident_id = 9001;
COMMIT;

-- STEP 4  ** SESSION B (Vikram) **
-- Vikram writes the value he computed from the NULL he read in step 2.
-- He never learns that Sneha wrote anything.
UPDATE incident
   SET root_cause = 'Replica disk I/O saturated.'
 WHERE incident_id = 9001;
COMMIT;

-- STEP 5  ** EITHER SESSION **
SELECT root_cause AS final_value FROM incident WHERE incident_id = 9001;
-- expect: 'Replica disk I/O saturated.'
--
-- Sneha's finding is GONE. Both transactions committed successfully.
-- The database is not corrupt - every constraint still holds. The data is
-- simply wrong, because the application logic assumed nothing else would
-- write between its read and its write.


-- =====================================================================
-- DEMO 2 - the cure. SELECT ... FOR UPDATE.
--
-- FOR UPDATE takes an exclusive row lock at READ time, not at write time.
-- The second session blocks on its SELECT until the first commits, and
-- then reads the CURRENT value - so it can append instead of overwrite.
-- =====================================================================

-- Reset
UPDATE incident SET root_cause = NULL WHERE incident_id = 9001;

-- STEP 1  ** SESSION A (Sneha) **
START TRANSACTION;
SELECT root_cause AS sneha_reads
  FROM incident
 WHERE incident_id = 9001
   FOR UPDATE;                 -- <-- exclusive lock on this row
-- expect: NULL

-- STEP 2  ** SESSION B (Vikram) **
START TRANSACTION;
SELECT root_cause AS vikram_reads
  FROM incident
 WHERE incident_id = 9001
   FOR UPDATE;
-- ** THIS BLOCKS ** - the client appears to hang. That is correct
-- behaviour: B is waiting for A's lock. Leave it waiting and go to step 3.

-- STEP 3  ** SESSION A (Sneha) **
UPDATE incident
   SET root_cause = 'Connection pool exhausted under load.'
 WHERE incident_id = 9001;
COMMIT;                        -- <-- releases the lock

-- STEP 4  ** SESSION B (Vikram) **
-- The blocked SELECT in step 2 now RETURNS, and it returns Sneha's
-- committed value, not NULL. Vikram can see he is not first.
-- expect: 'Connection pool exhausted under load.'
--
-- Now he appends instead of overwriting:
UPDATE incident
   SET root_cause = CONCAT(root_cause, ' Also: replica disk I/O saturated.')
 WHERE incident_id = 9001;
COMMIT;

-- STEP 5  ** EITHER SESSION **
SELECT root_cause AS final_value FROM incident WHERE incident_id = 9001;
-- expect: 'Connection pool exhausted under load. Also: replica disk I/O saturated.'
-- BOTH findings survived.


-- =====================================================================
-- DEMO 3 - the other cure: make the write atomic, so there is no window.
--
-- The lost update exists because the value travelled out to the client and
-- back. If the new value is computed IN the UPDATE statement, the read and
-- the write are one atomic operation and no interleaving is possible -
-- no explicit locking needed.
-- =====================================================================

UPDATE incident SET root_cause = NULL WHERE incident_id = 9001;

-- Either session can run these in any order, even simultaneously.
-- Both findings always survive.

-- ** SESSION A **
UPDATE incident
   SET root_cause = CONCAT(COALESCE(root_cause, ''), 'Connection pool exhausted. ')
 WHERE incident_id = 9001;

-- ** SESSION B **
UPDATE incident
   SET root_cause = CONCAT(COALESCE(root_cause, ''), 'Replica disk I/O saturated. ')
 WHERE incident_id = 9001;

SELECT root_cause AS final_value FROM incident WHERE incident_id = 9001;
-- Both fragments present, in whichever order the writes landed.
-- InnoDB takes the row lock for the duration of each UPDATE automatically.


-- =====================================================================
-- WHY REPEATABLE READ DOES NOT SAVE YOU
--
-- A common misreading: "MySQL defaults to REPEATABLE READ, so I am safe."
-- REPEATABLE READ guarantees your READS are consistent. It says nothing
-- about whether your WRITE is based on a value that is still current.
-- Demo 1 above runs at REPEATABLE READ and still loses the update.
--
-- Preventing a lost update needs one of:
--   1. a locking read              - SELECT ... FOR UPDATE   (demo 2)
--   2. an atomic read-modify-write - compute inside UPDATE    (demo 3)
--   3. optimistic concurrency      - a version column, and
--                                    UPDATE ... WHERE version = <the one
--                                    I read>, then check rows affected
--   4. SERIALIZABLE isolation      - correct, but the most contended and
--                                    slowest option
--
-- InfraTrace's own writes are mostly append-only inserts (deployments,
-- incidents, dependency edges), which do not suffer this problem. The
-- vulnerable operations are the in-place edits: incident.root_cause,
-- incident.status, component.criticality and component.owner_team_id.
-- Those are exactly where a locking read belongs.
-- =====================================================================

-- Reset the demo row
UPDATE incident SET root_cause = NULL WHERE incident_id = 9001;
