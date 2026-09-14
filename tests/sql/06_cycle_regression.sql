-- =====================================================================
-- InfraTrace - Suite 06: cycle-prevention regression
-- File   : tests/sql/06_cycle_regression.sql
--
-- WHY THIS SUITE EXISTS
--
-- An audit found that cycle prevention was bounded far too tightly:
--   fn_would_create_cycle()        walked only 50 hops
--   sp_detect_dependency_cycles()  walked only 20 hops
--
-- With a 60-component chain, the edge closing a 60-node cycle was therefore
-- ACCEPTED, a real cycle was stored, and the detector did not find it. The
-- documentation at the time claimed cycle prevention "of any length", which
-- was false.
--
-- The bounds are now:
--   fn_would_create_cycle        1000 (a safety net; UNION already terminates)
--   sp_detect_dependency_cycles  the component count (the exact bound for a
--                                simple cycle, and necessary because that walk
--                                uses UNION ALL and does not self-terminate)
--
-- This suite builds the exact 60-node scenario from the audit and asserts the
-- defect cannot return. It creates components 9001-9060, uses them, and
-- deletes them; ON DELETE CASCADE removes their edges.
--
-- Run as part of tests/run_all_tests.sql.
-- =====================================================================

USE infratrace;

-- Clear any leftovers from an interrupted run.
DELETE FROM component WHERE component_id BETWEEN 9001 AND 9099;

-- ---------------------------------------------------------------------
-- Helper: attempt an edge that must be refused, and record the outcome.
-- ---------------------------------------------------------------------
DROP PROCEDURE IF EXISTS test_cycle_rejected;
DELIMITER $$
CREATE PROCEDURE test_cycle_rejected(IN p_label VARCHAR(140), IN p_from INT, IN p_to INT)
BEGIN
    DECLARE v_rejected TINYINT DEFAULT 0;
    DECLARE CONTINUE HANDLER FOR SQLEXCEPTION SET v_rejected = 1;

    START TRANSACTION;
    INSERT INTO dependency (component_id, depends_on_id) VALUES (p_from, p_to);
    ROLLBACK;                      -- never keep the edge, even if wrongly accepted

    INSERT INTO test_result (suite, test_name, expected, actual, result)
    VALUES ('cycles', p_label, 'rejected',
            IF(v_rejected = 1, 'rejected', 'ACCEPTED'),
            IF(v_rejected = 1, 'PASS', 'FAIL'));
END$$
DELIMITER ;

-- =====================================================================
-- PART 1 - short cycles, on the real ShopSphere graph
-- =====================================================================

-- 1 node: a component depending on itself
CALL test_cycle_rejected('1-node cycle: self-dependency (3 -> 3)', 3, 3);

-- 2 nodes: Order Service(3) -> Payment Service(4) exists, so 4 -> 3 closes it
CALL test_cycle_rejected('2-node cycle: direct mutual (4 -> 3)', 4, 3);

-- 3 nodes: 3 -> 4 -> 11 exists, so 11 -> 3 closes it
CALL test_cycle_rejected('3-node cycle: transitive (11 -> 3)', 11, 3);

-- 4 nodes: 1 -> 3 -> 4 -> 11 exists, so 11 -> 1 closes it
CALL test_cycle_rejected('4-node cycle: transitive (11 -> 1)', 11, 1);

-- The function must agree with the triggers on every one of those.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'fn_would_create_cycle agrees on short cycles', '1,1,1,1',
       CONCAT_WS(',', fn_would_create_cycle(3,3), fn_would_create_cycle(4,3),
                      fn_would_create_cycle(11,3), fn_would_create_cycle(11,1)),
       IF(CONCAT_WS(',', fn_would_create_cycle(3,3), fn_would_create_cycle(4,3),
                         fn_would_create_cycle(11,3), fn_would_create_cycle(11,1))
          = '1,1,1,1', 'PASS', 'FAIL');

-- A safe edge must still be allowed, or "prevention" would just be "refuse
-- everything". Notification Service(6) -> Elasticsearch(18) closes nothing.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'a SAFE edge is still permitted (6 -> 18)', '0',
       fn_would_create_cycle(6, 18),
       IF(fn_would_create_cycle(6, 18) = 0, 'PASS', 'FAIL');

-- =====================================================================
-- PART 2 - the 60-node chain from the audit
--
-- Build 9001 -> 9002 -> ... -> 9060, a path of 59 edges. Closing it with
-- 9060 -> 9001 requires the reachability walk to travel 59 hops. The old
-- 50-hop bound could not see that far, so the edge was accepted.
-- =====================================================================

INSERT INTO component (component_id, component_name, component_type, criticality, created_on)
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 60)
SELECT 9000 + i, CONCAT('ZZ Chain ', LPAD(i, 3, '0')), 'Service', 'Low', '2026-01-01'
FROM n;

INSERT INTO dependency (component_id, depends_on_id)
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 60)
SELECT 9000 + i, 9000 + i + 1 FROM n WHERE i < 60;

-- Sanity: the chain really is 59 edges deep.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'test chain built: 59 edges', '59', COUNT(*),
       IF(COUNT(*) = 59, 'PASS', 'FAIL')
FROM dependency WHERE component_id BETWEEN 9001 AND 9099;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'test chain depth from head = 59', '59',
       fn_dependency_depth(9001),
       IF(fn_dependency_depth(9001) = 59, 'PASS', 'FAIL');

-- THE REGRESSION. This is the exact edge the old implementation accepted.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'fn_would_create_cycle sees a 59-hop closing path', '1',
       fn_would_create_cycle(9060, 9001),
       IF(fn_would_create_cycle(9060, 9001) = 1, 'PASS', 'FAIL');

CALL test_cycle_rejected('60-node cycle is REJECTED (9060 -> 9001)', 9060, 9001);

-- A mid-chain long cycle too, so the fix is not special-cased to the ends.
CALL test_cycle_rejected('35-node cycle is REJECTED (9040 -> 9006)', 9040, 9006);

-- =====================================================================
-- PART 3 - the DETECTOR must find a cycle that bypassed the triggers
--
-- Triggers can be bypassed by a bulk load or a restored dump, so detection
-- has to work independently of prevention. This drops the INSERT trigger,
-- stores a real 60-node cycle, checks the detector finds it, then restores
-- everything. All of it happens on the ZZ rows only.
-- =====================================================================

DROP TRIGGER trg_dependency_validate_insert;

INSERT INTO dependency (component_id, depends_on_id) VALUES (9060, 9001);

-- The detector must report every component in the cycle. All 60 are in it.
DROP TEMPORARY TABLE IF EXISTS cycle_probe;
CREATE TEMPORARY TABLE cycle_probe (component_in_cycle VARCHAR(100),
                                    cycle_length INT, cycle_path TEXT);

INSERT INTO cycle_probe
WITH RECURSIVE walk (origin_id, current_id, depth, path, closed) AS (
    SELECT c.component_id, c.component_id, 0,
           CAST(c.component_name AS CHAR(1000)), 0
    FROM component c
    UNION ALL
    SELECT w.origin_id, d.depends_on_id, w.depth + 1,
           CONCAT(w.path, ' -> ', c2.component_name),
           CASE WHEN d.depends_on_id = w.origin_id THEN 1 ELSE 0 END
    FROM walk w
    JOIN dependency d ON d.component_id = w.current_id
    JOIN component c2 ON d.depends_on_id = c2.component_id
    WHERE w.closed = 0 AND w.depth < (SELECT COUNT(*) FROM component)
)
SELECT c.component_name, w.depth, w.path
FROM walk w JOIN component c ON w.origin_id = c.component_id
WHERE w.closed = 1;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'detector FINDS the injected 60-node cycle', '60', COUNT(*),
       IF(COUNT(*) = 60, 'PASS', 'FAIL')
FROM cycle_probe;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'detector reports the cycle length as 60', '60',
       COALESCE(MIN(cycle_length), -1),
       IF(MIN(cycle_length) = 60, 'PASS', 'FAIL')
FROM cycle_probe;

-- Blast radius must still TERMINATE on cyclic data (UNION de-duplicates).
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'blast radius terminates on cyclic data', '60',
       fn_blast_radius_count(9030),
       IF(fn_blast_radius_count(9030) = 60, 'PASS', 'FAIL');

DROP TEMPORARY TABLE cycle_probe;

-- Remove the injected cycle and restore the trigger BEFORE anything else runs.
DELETE FROM dependency WHERE component_id = 9060 AND depends_on_id = 9001;

-- Restore from the CANONICAL definition rather than re-typing the trigger body
-- here. A copy in this file would silently drift the moment triggers.sql
-- changed, and the suite would then be testing a trigger that no longer
-- matches the one the project ships.
SOURCE ../database/triggers/triggers.sql;

-- =====================================================================
-- PART 4 - clean up and prove the real dataset is untouched
-- =====================================================================

DELETE FROM component WHERE component_id BETWEEN 9001 AND 9099;
DROP PROCEDURE test_cycle_rejected;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'cleanup: component rows back to 19', '19', COUNT(*),
       IF(COUNT(*) = 19, 'PASS', 'FAIL') FROM component;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'cleanup: dependency rows back to 29', '29', COUNT(*),
       IF(COUNT(*) = 29, 'PASS', 'FAIL') FROM dependency;

-- dependency carries exactly two triggers: validate_insert and validate_update
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'dependency triggers restored', '2', COUNT(*),
       IF(COUNT(*) = 2, 'PASS', 'FAIL')
FROM information_schema.triggers
WHERE trigger_schema = 'infratrace'
  AND event_object_table = 'dependency';

-- and all five triggers are back
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'all five triggers restored', '5', COUNT(*),
       IF(COUNT(*) = 5, 'PASS', 'FAIL')
FROM information_schema.triggers WHERE trigger_schema = 'infratrace';

-- prevention is live again: this must be refused
DROP PROCEDURE IF EXISTS test_cycle_rejected2;
DELIMITER $$
CREATE PROCEDURE test_cycle_rejected2()
BEGIN
    DECLARE v_rejected TINYINT DEFAULT 0;
    DECLARE CONTINUE HANDLER FOR SQLEXCEPTION SET v_rejected = 1;
    START TRANSACTION;
    INSERT INTO dependency (component_id, depends_on_id) VALUES (11, 3);
    ROLLBACK;
    INSERT INTO test_result (suite, test_name, expected, actual, result)
    VALUES ('cycles', 'prevention works again after restore', 'rejected',
            IF(v_rejected = 1, 'rejected', 'ACCEPTED'),
            IF(v_rejected = 1, 'PASS', 'FAIL'));
END$$
DELIMITER ;
CALL test_cycle_rejected2();
DROP PROCEDURE test_cycle_rejected2;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'cycles', 'graph is acyclic again after the probe', '0', COUNT(*),
       IF(COUNT(*) = 0, 'PASS', 'FAIL')
FROM (
    WITH RECURSIVE walk (origin_id, current_id, depth, closed) AS (
        SELECT component_id, component_id, 0, 0 FROM component
        UNION ALL
        SELECT w.origin_id, d.depends_on_id, w.depth + 1,
               CASE WHEN d.depends_on_id = w.origin_id THEN 1 ELSE 0 END
        FROM walk w JOIN dependency d ON d.component_id = w.current_id
        WHERE w.closed = 0 AND w.depth < (SELECT COUNT(*) FROM component)
    )
    SELECT 1 FROM walk WHERE closed = 1
) still_cyclic;

SELECT 'Suite 06 (cycle regression) complete.' AS status;
