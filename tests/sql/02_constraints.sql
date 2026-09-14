-- =====================================================================
-- InfraTrace - Suite 02: constraints and triggers actually REJECT bad data
-- File   : tests/sql/02_constraints.sql
--
-- Counting constraints only proves they were declared. These tests prove
-- they FIRE, by attempting a bad write inside a handler and recording
-- whether the database refused it.
--
-- Every test is wrapped in a stored procedure with a CONTINUE HANDLER, so
-- a rejection is captured as a PASS instead of aborting the suite. Each
-- procedure also rolls back, so the dataset is never modified.
-- =====================================================================

USE infratrace;

DROP PROCEDURE IF EXISTS test_expect_rejection;

DELIMITER $$
-- Runs one statement that is SUPPOSED to fail.
-- p_label  : what is being tested
-- p_sql    : the offending statement
-- Records PASS if the database raised an error, FAIL if it accepted the row.
CREATE PROCEDURE test_expect_rejection(IN p_label VARCHAR(140), IN p_sql TEXT)
BEGIN
    DECLARE v_rejected TINYINT DEFAULT 0;
    DECLARE CONTINUE HANDLER FOR SQLEXCEPTION SET v_rejected = 1;

    START TRANSACTION;
    SET @stmt = p_sql;
    PREPARE s FROM @stmt;
    EXECUTE s;
    DEALLOCATE PREPARE s;
    ROLLBACK;          -- never keep the row, even if it was wrongly accepted

    INSERT INTO test_result (suite, test_name, expected, actual, result)
    VALUES ('constraints', p_label, 'rejected',
            IF(v_rejected = 1, 'rejected', 'ACCEPTED'),
            IF(v_rejected = 1, 'PASS', 'FAIL'));
END$$
DELIMITER ;

-- --- CHECK constraints ------------------------------------------------
CALL test_expect_rejection('CHECK: incident severity must be SEV1..SEV4',
  'INSERT INTO incident (title,severity,status,environment_id,started_at)
   VALUES (''t'',''SEV9'',''Open'',3,''2026-09-10 10:00:00'')');

CALL test_expect_rejection('CHECK: component_type must be a known type',
  'INSERT INTO component (component_name,component_type,criticality,created_on)
   VALUES (''t'',''Wormhole'',''Low'',''2026-01-01'')');

CALL test_expect_rejection('CHECK: criticality must be Low..Critical',
  'INSERT INTO component (component_name,component_type,criticality,created_on)
   VALUES (''t2'',''Service'',''Apocalyptic'',''2026-01-01'')');

CALL test_expect_rejection('CHECK: deployment status must be Success or Failed',
  'INSERT INTO deployment (component_id,environment_id,version,deployed_at,status)
   VALUES (3,2,''v9.9.9'',''2027-01-01 00:00:00'',''RolledBack'')');

CALL test_expect_rejection('CHECK: resolved_at cannot precede started_at',
  'INSERT INTO incident (title,severity,status,environment_id,started_at,resolved_at)
   VALUES (''t'',''SEV3'',''Resolved'',3,''2026-09-10 10:00:00'',''2026-09-09 10:00:00'')');

CALL test_expect_rejection('CHECK: a Resolved incident must carry resolved_at',
  'INSERT INTO incident (title,severity,status,environment_id,started_at)
   VALUES (''t'',''SEV3'',''Resolved'',3,''2026-09-10 10:00:00'')');

-- --- UNIQUE constraints -----------------------------------------------
CALL test_expect_rejection('UNIQUE: duplicate component_name',
  'INSERT INTO component (component_name,component_type,criticality,created_on)
   VALUES (''Payment DB'',''Database'',''High'',''2026-01-01'')');

CALL test_expect_rejection('UNIQUE: duplicate dependency edge',
  'INSERT INTO dependency (component_id,depends_on_id) VALUES (3,4)');

CALL test_expect_rejection('UNIQUE: duplicate deployment event (natural key)',
  'INSERT INTO deployment (component_id,environment_id,version,deployed_at)
   VALUES (3,3,''v5.1.0'',''2026-06-08 09:30:00'')');

CALL test_expect_rejection('UNIQUE: duplicate team email',
  'INSERT INTO team (team_name,team_email,created_on)
   VALUES (''New Team'',''payments@shopsphere.io'',''2026-01-01'')');

-- --- FOREIGN KEYS -----------------------------------------------------
CALL test_expect_rejection('FK: deployment to a non-existent component',
  'INSERT INTO deployment (component_id,environment_id,version,deployed_at)
   VALUES (9999,3,''v1'',''2027-02-02 00:00:00'')');

CALL test_expect_rejection('FK: incident in a non-existent environment',
  'INSERT INTO incident (title,severity,status,environment_id,started_at)
   VALUES (''t'',''SEV3'',''Open'',9999,''2026-09-10 10:00:00'')');

CALL test_expect_rejection('FK: dependency on a non-existent component',
  'INSERT INTO dependency (component_id,depends_on_id) VALUES (3,9999)');

-- --- NOT NULL ---------------------------------------------------------
CALL test_expect_rejection('NOT NULL: component_name is required',
  'INSERT INTO component (component_name,component_type,criticality,created_on)
   VALUES (NULL,''Service'',''Low'',''2026-01-01'')');

-- --- TRIGGERS: dependency graph validation ----------------------------
CALL test_expect_rejection('TRIGGER: self-dependency A -> A',
  'INSERT INTO dependency (component_id,depends_on_id) VALUES (3,3)');

CALL test_expect_rejection('TRIGGER: direct mutual cycle A <-> B',
  'INSERT INTO dependency (component_id,depends_on_id) VALUES (4,3)');

-- Order Service(3) -> Payment Service(4) -> Payment DB(11) already exists,
-- so 11 -> 3 would close a three-node cycle.
CALL test_expect_rejection('TRIGGER: transitive cycle A -> B -> C -> A',
  'INSERT INTO dependency (component_id,depends_on_id) VALUES (11,3)');

-- API Gateway(1) -> Order Service(3) -> Payment Service(4) -> Payment DB(11),
-- so 11 -> 1 would close a four-node cycle.
CALL test_expect_rejection('TRIGGER: four-node cycle rejected',
  'INSERT INTO dependency (component_id,depends_on_id) VALUES (11,1)');

CALL test_expect_rejection('TRIGGER: UPDATE cannot create a self-dependency',
  'UPDATE dependency SET depends_on_id = component_id WHERE dependency_id = 1');

-- --- TRIGGERS: deployment validation ---------------------------------
-- Legacy Coupon Service (10) is retired; environment 3 is Production.
CALL test_expect_rejection('TRIGGER: retired component cannot be INSERTed to production',
  'INSERT INTO deployment (component_id,environment_id,version,deployed_at)
   VALUES (10,3,''v1.0.0'',''2027-03-03 00:00:00'')');

CALL test_expect_rejection('TRIGGER: retired component cannot be UPDATEd into production',
  'UPDATE deployment SET component_id = 10, environment_id = 3 WHERE deployment_id = 35');

DROP PROCEDURE test_expect_rejection;

-- --- the dataset must be untouched by all of the above ----------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'constraints', 'dataset unchanged: dependency rows', '29', COUNT(*),
       IF(COUNT(*) = 29, 'PASS', 'FAIL') FROM dependency;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'constraints', 'dataset unchanged: deployment rows', '46', COUNT(*),
       IF(COUNT(*) = 46, 'PASS', 'FAIL') FROM deployment;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'constraints', 'dataset unchanged: incident rows', '22', COUNT(*),
       IF(COUNT(*) = 22, 'PASS', 'FAIL') FROM incident;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'constraints', 'dataset unchanged: component rows', '19', COUNT(*),
       IF(COUNT(*) = 19, 'PASS', 'FAIL') FROM component;

-- --- TRIGGER: incident resolution timestamp is maintained ------------
-- Done as a real UPDATE inside a transaction that is rolled back.
START TRANSACTION;
UPDATE incident SET status = 'Resolved' WHERE incident_id = 12;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'constraints', 'TRIGGER: closing an incident stamps resolved_at',
       'not null', IF(resolved_at IS NULL, 'null', 'not null'),
       IF(resolved_at IS NOT NULL, 'PASS', 'FAIL')
FROM incident WHERE incident_id = 12;

UPDATE incident SET status = 'Open' WHERE incident_id = 12;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'constraints', 'TRIGGER: reopening an incident clears resolved_at',
       'null', IF(resolved_at IS NULL, 'null', 'not null'),
       IF(resolved_at IS NULL, 'PASS', 'FAIL')
FROM incident WHERE incident_id = 12;
ROLLBACK;

SELECT 'Suite 02 (constraints) complete.' AS status;
