-- =====================================================================
-- InfraTrace - Suite 03: data integrity and seed consistency
-- File   : tests/sql/03_integrity.sql
--
-- Checks the loaded dataset is internally consistent, and that the
-- semantic invariants the analysis relies on actually hold.
-- =====================================================================

USE infratrace;

-- --- row counts -------------------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'team rows', '6', COUNT(*), IF(COUNT(*)=6,'PASS','FAIL') FROM team;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'developer rows', '15', COUNT(*), IF(COUNT(*)=15,'PASS','FAIL') FROM developer;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'application rows', '4', COUNT(*), IF(COUNT(*)=4,'PASS','FAIL') FROM application;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'component rows', '19', COUNT(*), IF(COUNT(*)=19,'PASS','FAIL') FROM component;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'application_component rows', '17', COUNT(*), IF(COUNT(*)=17,'PASS','FAIL') FROM application_component;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'dependency rows', '29', COUNT(*), IF(COUNT(*)=29,'PASS','FAIL') FROM dependency;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'environment rows', '3', COUNT(*), IF(COUNT(*)=3,'PASS','FAIL') FROM environment;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'deployment rows', '46', COUNT(*), IF(COUNT(*)=46,'PASS','FAIL') FROM deployment;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'incident rows', '22', COUNT(*), IF(COUNT(*)=22,'PASS','FAIL') FROM incident;
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'incident_component rows', '59', COUNT(*), IF(COUNT(*)=59,'PASS','FAIL') FROM incident_component;

-- --- graph invariants -------------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'no self-dependency rows', '0', COUNT(*), IF(COUNT(*)=0,'PASS','FAIL')
FROM dependency WHERE component_id = depends_on_id;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'no mutual dependency pairs', '0', COUNT(*), IF(COUNT(*)=0,'PASS','FAIL')
FROM dependency a JOIN dependency b
  ON a.component_id = b.depends_on_id AND a.depends_on_id = b.component_id;

-- The strong version: the whole graph must be acyclic at any cycle length
-- the recursion limit allows. sp_detect_dependency_cycles returns rows only
-- if a cycle exists, so it is re-implemented here as a countable expression.
-- The depth bound is the component count because a SIMPLE cycle cannot visit
-- more components than exist; this walk uses UNION ALL and so would not
-- terminate on its own if the data were cyclic.
INSERT INTO test_result (suite, test_name, expected, actual, result)
WITH RECURSIVE walk (origin_id, current_id, depth, closed) AS (
    SELECT component_id, component_id, 0, 0 FROM component
    UNION ALL
    SELECT w.origin_id, d.depends_on_id, w.depth + 1,
           CASE WHEN d.depends_on_id = w.origin_id THEN 1 ELSE 0 END
    FROM walk w JOIN dependency d ON d.component_id = w.current_id
    WHERE w.closed = 0 AND w.depth < (SELECT COUNT(*) FROM component)
)
SELECT 'integrity', 'dependency graph is acyclic (up to N-component cycles)', '0',
       COUNT(*), IF(COUNT(*)=0,'PASS','FAIL')
FROM walk WHERE closed = 1;

-- --- incident invariants ---------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'every incident has >=1 affected component', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM incident i
WHERE NOT EXISTS (SELECT 1 FROM incident_component ic WHERE ic.incident_id = i.incident_id);

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'every incident has exactly one RootCause component', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM (
    SELECT i.incident_id
    FROM incident i
    LEFT JOIN incident_component ic
           ON i.incident_id = ic.incident_id AND ic.impact_level = 'RootCause'
    GROUP BY i.incident_id
    HAVING COUNT(ic.component_id) <> 1
) bad;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'no resolved incident missing resolved_at', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM incident WHERE status = 'Resolved' AND resolved_at IS NULL;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'no open incident carrying a resolved_at', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM incident WHERE status <> 'Resolved' AND resolved_at IS NOT NULL;

-- A confirmed cause must name a deployment of a component the incident
-- actually affected - otherwise the causal link is nonsense.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'confirmed cause deployment belongs to an affected component', '0',
       COUNT(*), IF(COUNT(*)=0,'PASS','FAIL')
FROM incident i
JOIN deployment d ON i.caused_by_deployment_id = d.deployment_id
WHERE NOT EXISTS (
    SELECT 1 FROM incident_component ic
    WHERE ic.incident_id = i.incident_id AND ic.component_id = d.component_id);

-- A confirmed cause must also PRECEDE the incident it caused.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'confirmed cause deployment precedes its incident', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM incident i
JOIN deployment d ON i.caused_by_deployment_id = d.deployment_id
WHERE d.deployed_at >= i.started_at;

-- --- deployment invariants -------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'no retired component deployed to production', '0', COUNT(*),
       IF(COUNT(*)=0,'PASS','FAIL')
FROM deployment d
JOIN component   c ON d.component_id   = c.component_id
JOIN environment e ON d.environment_id = e.environment_id
WHERE c.is_active = 0 AND e.is_production = 1;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'exactly one rollback deployment in the dataset', '1', COUNT(*),
       IF(COUNT(*)=1,'PASS','FAIL')
FROM deployment WHERE is_rollback = 1;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'at least one failed deployment exists', 'yes',
       IF(COUNT(*) > 0, 'yes', 'no'), IF(COUNT(*) > 0, 'PASS', 'FAIL')
FROM deployment WHERE status = 'Failed';

-- --- deliberate edge cases the analysis is meant to surface -----------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'edge case: unowned components present', '2', COUNT(*),
       IF(COUNT(*)=2,'PASS','FAIL') FROM component WHERE owner_team_id IS NULL;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'edge case: retired component present', '1', COUNT(*),
       IF(COUNT(*)=1,'PASS','FAIL') FROM component WHERE is_active = 0;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'edge case: open (unresolved) incidents present', '3', COUNT(*),
       IF(COUNT(*)=3,'PASS','FAIL') FROM incident WHERE status <> 'Resolved';

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'edge case: components with zero dependents present', 'yes',
       IF(COUNT(*) > 0,'yes','no'), IF(COUNT(*) > 0,'PASS','FAIL')
FROM component c
WHERE NOT EXISTS (SELECT 1 FROM dependency d WHERE d.depends_on_id = c.component_id);

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'edge case: all three environments used by deployments', '3',
       COUNT(DISTINCT environment_id), IF(COUNT(DISTINCT environment_id)=3,'PASS','FAIL')
FROM deployment;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'edge case: all four severities used by incidents', '4',
       COUNT(DISTINCT severity), IF(COUNT(DISTINCT severity)=4,'PASS','FAIL')
FROM incident;

SELECT 'Suite 03 (integrity) complete.' AS status;
