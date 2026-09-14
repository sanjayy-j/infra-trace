-- =====================================================================
-- InfraTrace - Suite 04: functions, procedures and views return correct values
-- File   : tests/sql/04_stored_programs.sql
--
-- These are value tests, not existence tests. Each expected number was
-- derived by hand from the seed dataset, so a wrong refactor of a
-- recursive query is caught rather than silently accepted.
-- =====================================================================

USE infratrace;

-- --- fn_direct_dependent_count ---------------------------------------
-- Kafka Event Bus (16) is depended on by components 2,3,4,6,7,9 = 6
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_direct_dependent_count(Kafka) = 6', '6',
       fn_direct_dependent_count(16), IF(fn_direct_dependent_count(16)=6,'PASS','FAIL');

-- Payment DB (11) is depended on only by Payment Service = 1
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_direct_dependent_count(Payment DB) = 1', '1',
       fn_direct_dependent_count(11), IF(fn_direct_dependent_count(11)=1,'PASS','FAIL');

-- API Gateway (1) is the entry point: nothing depends on it
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_direct_dependent_count(API Gateway) = 0', '0',
       fn_direct_dependent_count(1), IF(fn_direct_dependent_count(1)=0,'PASS','FAIL');

-- --- fn_blast_radius_count -------------------------------------------
-- Payment DB -> Payment Service -> Order Service -> API Gateway = 3
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_blast_radius_count(Payment DB) = 3', '3',
       fn_blast_radius_count(11), IF(fn_blast_radius_count(11)=3,'PASS','FAIL');

-- Kafka reaches 2,3,4,6,7,9 then API Gateway = 7
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_blast_radius_count(Kafka) = 7', '7',
       fn_blast_radius_count(16), IF(fn_blast_radius_count(16)=7,'PASS','FAIL');

-- Redis reaches 2,4,5,8 then 1,3 = 6
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_blast_radius_count(Redis) = 6', '6',
       fn_blast_radius_count(15), IF(fn_blast_radius_count(15)=6,'PASS','FAIL');

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_blast_radius_count(API Gateway) = 0', '0',
       fn_blast_radius_count(1), IF(fn_blast_radius_count(1)=0,'PASS','FAIL');

-- blast radius must never exceed the number of other components
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'no blast radius exceeds component count - 1', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM component c
WHERE fn_blast_radius_count(c.component_id) > (SELECT COUNT(*) - 1 FROM component);

-- --- fn_dependency_depth ---------------------------------------------
-- API Gateway -> service -> service -> DB = 3 levels
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_dependency_depth(API Gateway) = 3', '3',
       fn_dependency_depth(1), IF(fn_dependency_depth(1)=3,'PASS','FAIL');

-- Payment DB depends on nothing
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_dependency_depth(Payment DB) = 0', '0',
       fn_dependency_depth(11), IF(fn_dependency_depth(11)=0,'PASS','FAIL');

-- --- fn_incident_count -----------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_incident_count matches incident_component', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM component c
WHERE fn_incident_count(c.component_id) <>
      (SELECT COUNT(*) FROM incident_component ic WHERE ic.component_id = c.component_id);

-- --- fn_risk_score ---------------------------------------------------
-- Kafka: blast 7 x Critical weight 4 = 28
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_risk_score(Kafka) = 28', '28',
       fn_risk_score(16), IF(fn_risk_score(16)=28,'PASS','FAIL');

-- Payment DB: blast 3 x Critical weight 4 = 12
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_risk_score(Payment DB) = 12', '12',
       fn_risk_score(11), IF(fn_risk_score(11)=12,'PASS','FAIL');

-- --- fn_would_create_cycle -------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_would_create_cycle: self-dependency = 1', '1',
       fn_would_create_cycle(3,3), IF(fn_would_create_cycle(3,3)=1,'PASS','FAIL');

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_would_create_cycle: direct mutual (4->3) = 1', '1',
       fn_would_create_cycle(4,3), IF(fn_would_create_cycle(4,3)=1,'PASS','FAIL');

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_would_create_cycle: transitive (11->3) = 1', '1',
       fn_would_create_cycle(11,3), IF(fn_would_create_cycle(11,3)=1,'PASS','FAIL');

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_would_create_cycle: four-node (11->1) = 1', '1',
       fn_would_create_cycle(11,1), IF(fn_would_create_cycle(11,1)=1,'PASS','FAIL');

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'functions', 'fn_would_create_cycle: safe edge (6->15) = 0', '0',
       fn_would_create_cycle(6,15), IF(fn_would_create_cycle(6,15)=0,'PASS','FAIL');

-- --- views -----------------------------------------------------------
-- A view must never multiply rows. One row per key is the invariant.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'component_ownership_view: one row per component', 'no duplicates',
       IF(COUNT(*) = COUNT(DISTINCT component_id), 'no duplicates', 'DUPLICATES'),
       IF(COUNT(*) = COUNT(DISTINCT component_id), 'PASS', 'FAIL')
FROM component_ownership_view;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'component_ownership_view: covers all 19 components', '19', COUNT(*),
       IF(COUNT(*)=19,'PASS','FAIL') FROM component_ownership_view;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'component_ownership_view: unowned shown as UNASSIGNED', '2', COUNT(*),
       IF(COUNT(*)=2,'PASS','FAIL')
FROM component_ownership_view WHERE owning_team = 'UNASSIGNED';

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'production_infrastructure_view: one row per component', 'no duplicates',
       IF(COUNT(*) = COUNT(DISTINCT component_id), 'no duplicates', 'DUPLICATES'),
       IF(COUNT(*) = COUNT(DISTINCT component_id), 'PASS', 'FAIL')
FROM production_infrastructure_view;

-- The rollback regression: Inventory Service was rolled back to v3.9.0,
-- so the view must NOT report the v4.0.0 that was rolled back.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'production view honours rollback (Inventory Service = v3.9.0)',
       'v3.9.0', running_version, IF(running_version='v3.9.0','PASS','FAIL')
FROM production_infrastructure_view WHERE component_name = 'Inventory Service';

-- production view must only ever show production, and only successes
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'production view contains only Production rows', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM production_infrastructure_view WHERE environment_name <> 'Production';

-- What the view actually guarantees, stated precisely.
--
-- It does NOT filter is_active, and that is deliberate: is_active is a
-- CATALOGUE state, while deployment rows are HISTORY. There is no "undeploy"
-- event in the schema, so a component retired after being deployed is still
-- running until someone removes it. The view reports what is running, not what
-- the catalogue wishes were running.
--
-- An earlier version of this test asserted "production view excludes retired
-- components". It passed, but only because the single retired component in the
-- dataset happens to have no production deployment - it would have passed even
-- if the view were broken. The assertion below tests the real invariant.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'production view shows only SUCCESSFUL production deployments', '0',
       COUNT(*), IF(COUNT(*)=0,'PASS','FAIL')
FROM production_infrastructure_view v
JOIN deployment d
  ON d.component_id = v.component_id
 AND d.deployed_at  = v.deployed_at
WHERE d.status <> 'Success';

-- The related rule that IS enforced, by trigger rather than by the view:
-- a retired component can never ACQUIRE a production deployment.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'no retired component has a production deployment (trigger-enforced)',
       '0', COUNT(*), IF(COUNT(*)=0,'PASS','FAIL')
FROM deployment d
JOIN component   c ON d.component_id   = c.component_id
JOIN environment e ON d.environment_id = e.environment_id
WHERE c.is_active = 0 AND e.is_production = 1;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'dependency_edge_view: one row per dependency', '29', COUNT(*),
       IF(COUNT(*)=29,'PASS','FAIL') FROM dependency_edge_view;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'incident_impact_view: one row per incident-component pair', '59', COUNT(*),
       IF(COUNT(*)=59,'PASS','FAIL') FROM incident_impact_view;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'views', 'incident_impact_view: resolution_minutes NULL only when unresolved',
       '0', COUNT(*), IF(COUNT(*)=0,'PASS','FAIL')
FROM incident_impact_view
WHERE (resolved_at IS NOT NULL AND resolution_minutes IS NULL)
   OR (resolved_at IS NULL AND resolution_minutes IS NOT NULL);

-- --- procedures execute without error --------------------------------
-- A procedure that raises would abort the script, so reaching the
-- recorded row is itself the assertion.
CALL sp_get_dependencies(3);
INSERT INTO test_result (suite, test_name, expected, actual, result)
VALUES ('procedures','sp_get_dependencies(3) executes','ok','ok','PASS');

CALL sp_get_blast_radius(11);
INSERT INTO test_result (suite, test_name, expected, actual, result)
VALUES ('procedures','sp_get_blast_radius(11) executes','ok','ok','PASS');

CALL sp_team_health_report(2);
INSERT INTO test_result (suite, test_name, expected, actual, result)
VALUES ('procedures','sp_team_health_report(2) executes','ok','ok','PASS');

CALL sp_component_impact_report(11);
INSERT INTO test_result (suite, test_name, expected, actual, result)
VALUES ('procedures','sp_component_impact_report(11) executes','ok','ok','PASS');

CALL sp_detect_dependency_cycles();
INSERT INTO test_result (suite, test_name, expected, actual, result)
VALUES ('procedures','sp_detect_dependency_cycles() executes','ok','ok','PASS');

SELECT 'Suite 04 (stored programs) complete.' AS status;
