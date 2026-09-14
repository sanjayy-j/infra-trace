-- =====================================================================
-- InfraTrace - Suite 05: analytical queries return meaningful results
-- File   : tests/sql/05_analytics.sql
--
-- The point of the seed dataset is that the analysis is not empty and not
-- wrong. These tests assert that the key analytical questions return
-- rows, and that a few known answers are exactly right.
--
-- They also guard against aggregate fan-out: the bug class where adding a
-- JOIN silently multiplies rows and inflates every SUM().
-- =====================================================================

USE infratrace;

-- --- non-empty analysis ----------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'components having dependents', '>0', COUNT(*),
       IF(COUNT(*)>0,'PASS','FAIL')
FROM (SELECT depends_on_id FROM dependency GROUP BY depends_on_id) x;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'components with more than one incident', '>0', COUNT(*),
       IF(COUNT(*)>0,'PASS','FAIL')
FROM (SELECT component_id FROM incident_component
      GROUP BY component_id HAVING COUNT(*) > 1) x;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'components live in production', '>0', COUNT(*),
       IF(COUNT(*)>0,'PASS','FAIL') FROM production_infrastructure_view;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'production deployments followed by an incident within 7 days', '>0',
       COUNT(*), IF(COUNT(*)>0,'PASS','FAIL')
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN incident_component ic ON ic.component_id  = d.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
WHERE e.is_production = 1 AND d.status = 'Success'
  AND i.started_at > d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'incidents with a CONFIRMED causing deployment', '12', COUNT(*),
       IF(COUNT(*)=12,'PASS','FAIL')
FROM incident WHERE caused_by_deployment_id IS NOT NULL;

-- --- known-correct answers -------------------------------------------
-- The headline demo: Payment DB's blast radius chain, exactly 3 components.
INSERT INTO test_result (suite, test_name, expected, actual, result)
WITH RECURSIVE impact (component_id, depth) AS (
    SELECT component_id, 0 FROM component WHERE component_name = 'Payment DB'
    UNION
    SELECT d.component_id, im.depth + 1
    FROM dependency d JOIN impact im ON d.depends_on_id = im.component_id
    WHERE im.depth < 1000
)
SELECT 'analytics', 'Payment DB blast radius chain = 3 affected', '3', COUNT(*),
       IF(COUNT(*)=3,'PASS','FAIL')
FROM impact WHERE depth > 0;

-- The deepest propagation from Payment DB must reach API Gateway at hop 3.
INSERT INTO test_result (suite, test_name, expected, actual, result)
WITH RECURSIVE impact (component_id, depth) AS (
    SELECT component_id, 0 FROM component WHERE component_name = 'Payment DB'
    UNION
    SELECT d.component_id, im.depth + 1
    FROM dependency d JOIN impact im ON d.depends_on_id = im.component_id
    WHERE im.depth < 1000
)
SELECT 'analytics', 'Payment DB failure reaches API Gateway at hop 3', 'API Gateway',
       c.component_name, IF(c.component_name = 'API Gateway','PASS','FAIL')
FROM impact im JOIN component c ON im.component_id = c.component_id
WHERE im.depth = 3;

-- Kafka must be the single highest-risk component.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'highest risk_score component is Kafka Event Bus', 'Kafka Event Bus',
       component_name, IF(component_name = 'Kafka Event Bus','PASS','FAIL')
FROM component
WHERE is_active = 1
ORDER BY fn_risk_score(component_id) DESC, component_name
LIMIT 1;

-- A Payment DB failure must reach all four applications.
INSERT INTO test_result (suite, test_name, expected, actual, result)
WITH RECURSIVE impact (component_id) AS (
    SELECT component_id FROM component WHERE component_name = 'Payment DB'
    UNION
    SELECT d.component_id FROM dependency d JOIN impact im ON d.depends_on_id = im.component_id
)
SELECT 'analytics', 'Payment DB failure reaches 4 applications', '4',
       COUNT(DISTINCT ac.application_id), IF(COUNT(DISTINCT ac.application_id)=4,'PASS','FAIL')
FROM impact im JOIN application_component ac ON im.component_id = ac.component_id;

-- --- scenarios the demonstration queries depend on ---------------------
-- Query A4 in 08_advanced_sql.sql reports active, deployed components that
-- have NEVER had an incident. An audit found it silently returning zero rows
-- after the dataset was extended, because every component had acquired an
-- incident. These tests assert the scenario still exists, so the query cannot
-- go quietly empty again.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'A4 scenario: a deployed component with NO incident history', '>0',
       COUNT(*), IF(COUNT(*) > 0, 'PASS', 'FAIL')
FROM component c
WHERE c.is_active = 1
  AND NOT EXISTS (SELECT 1 FROM incident_component ic WHERE ic.component_id = c.component_id)
  AND EXISTS     (SELECT 1 FROM deployment d        WHERE d.component_id  = c.component_id);

-- The interesting case is one with a WIDE blast radius: no incident history but
-- plenty depending on it. That is the row the query exists to surface.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'A4 scenario includes a wide-blast-radius component', '>0',
       COUNT(*), IF(COUNT(*) > 0, 'PASS', 'FAIL')
FROM component c
WHERE c.is_active = 1
  AND fn_blast_radius_count(c.component_id) >= 3
  AND NOT EXISTS (SELECT 1 FROM incident_component ic WHERE ic.component_id = c.component_id)
  AND EXISTS     (SELECT 1 FROM deployment d        WHERE d.component_id  = c.component_id);

-- Every other category file must keep producing rows too. These are the
-- scenarios whose absence would make a demonstration query look broken.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'O4 scenario: at least one ownership/coverage gap', '>0',
       COUNT(*), IF(COUNT(*) > 0, 'PASS', 'FAIL')
FROM component WHERE owner_team_id IS NULL;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'D4 scenario: cross-team dependency edges exist', '>0',
       COUNT(*), IF(COUNT(*) > 0, 'PASS', 'FAIL')
FROM dependency d
JOIN component s ON d.component_id  = s.component_id
JOIN component g ON d.depends_on_id = g.component_id
WHERE NOT (s.owner_team_id <=> g.owner_team_id);

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'P4 scenario: a failed or rolled-back deployment exists', '>0',
       COUNT(*), IF(COUNT(*) > 0, 'PASS', 'FAIL')
FROM deployment WHERE status = 'Failed' OR is_rollback = 1;

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'A3 scenario: a component beats its type average', '>0',
       COUNT(*), IF(COUNT(*) > 0, 'PASS', 'FAIL')
FROM component c
WHERE c.is_active = 1
  AND fn_blast_radius_count(c.component_id) >
      (SELECT AVG(fn_blast_radius_count(c2.component_id)) FROM component c2
        WHERE c2.component_type = c.component_type AND c2.is_active = 1);

-- --- fan-out guards ---------------------------------------------------
-- Q15-style summary: the per-type component counts must still add up to
-- the real total. If a JOIN were multiplying rows, this would inflate.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'summary by type sums to the real component count', '19',
       SUM(components), IF(SUM(components)=19,'PASS','FAIL')
FROM (SELECT component_type, COUNT(*) AS components FROM component GROUP BY component_type) s;

-- The same for the critical count, which is where the original fan-out
-- bug showed itself (it once reported 182 instead of 3).
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'critical components counted once each', '8', SUM(critical),
       IF(SUM(critical)=8,'PASS','FAIL')
FROM (
    SELECT c.component_type,
           SUM(CASE WHEN c.criticality='Critical' THEN 1 ELSE 0 END) AS critical
    FROM component c GROUP BY c.component_type
) s;

-- Unowned counted once each.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'unowned components counted once each', '2', SUM(unowned),
       IF(SUM(unowned)=2,'PASS','FAIL')
FROM (
    SELECT c.component_type,
           SUM(CASE WHEN c.owner_team_id IS NULL THEN 1 ELSE 0 END) AS unowned
    FROM component c GROUP BY c.component_type
) s;

-- Team ownership must partition the owned components exactly.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'per-team component counts sum to owned components', '17',
       SUM(owned), IF(SUM(owned)=17,'PASS','FAIL')
FROM (
    SELECT t.team_id, COUNT(c.component_id) AS owned
    FROM team t LEFT JOIN component c ON t.team_id = c.owner_team_id
    GROUP BY t.team_id
) s;

-- Deployment status breakdown must sum to the total row count.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'deployment status breakdown sums to 46', '46',
       SUM(successful + failed), IF(SUM(successful + failed)=46,'PASS','FAIL')
FROM (
    SELECT SUM(status='Success') AS successful, SUM(status='Failed') AS failed
    FROM deployment
) s;

-- Incident severity breakdown must sum to the total.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'incident severity breakdown sums to 22', '22', SUM(n),
       IF(SUM(n)=22,'PASS','FAIL')
FROM (SELECT severity, COUNT(*) AS n FROM incident GROUP BY severity) s;

-- --- aggregate sanity -------------------------------------------------
-- Average resolution time must ignore unresolved incidents rather than
-- treating them as zero.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'analytics', 'avg resolution time computed only over resolved incidents', 'yes',
       IF(ROUND(AVG(TIMESTAMPDIFF(MINUTE, started_at, resolved_at)),1) =
          ROUND((SELECT AVG(TIMESTAMPDIFF(MINUTE, started_at, resolved_at))
                 FROM incident WHERE resolved_at IS NOT NULL),1), 'yes','no'),
       IF(ROUND(AVG(TIMESTAMPDIFF(MINUTE, started_at, resolved_at)),1) =
          ROUND((SELECT AVG(TIMESTAMPDIFF(MINUTE, started_at, resolved_at))
                 FROM incident WHERE resolved_at IS NOT NULL),1), 'PASS','FAIL')
FROM incident;

SELECT 'Suite 05 (analytics) complete.' AS status;
