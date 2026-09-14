-- =====================================================================
-- InfraTrace - 09. DBMS feature demonstration
-- File   : database/queries/09_dbms_features_demo.sql
--
-- One script that exercises every stored program and then proves the
-- database REFUSES bad data, in the order you would present it.
--
-- Run with --force, because the last section deliberately violates
-- constraints and each violation prints an error:
--
--     cd database
--     mysql -u root -p --force --table infratrace < queries/09_dbms_features_demo.sql
--
-- Nothing here modifies the dataset: the trigger demo rolls back, and
-- every bad write is rejected.
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

-- =====================================================================
-- PART 1 - VIEWS (4)
-- =====================================================================

SELECT '=== VIEW 1/4: component_ownership_view - unowned stays VISIBLE ===' AS demo;
SELECT component_name, component_type, criticality, owning_team, owner_contact
FROM component_ownership_view
WHERE owning_team = 'UNASSIGNED'
   OR criticality = 'Critical'
ORDER BY owning_team, component_name;

SELECT '=== VIEW 2/4: production_infrastructure_view - what is LIVE now ===' AS demo;
-- Note Inventory Service: v3.9.0, not the v4.0.0 that was rolled back.
SELECT component_name, running_version, deployed_at, owning_team
FROM production_infrastructure_view
ORDER BY deployed_at DESC
LIMIT 10;

SELECT '=== VIEW 3/4: incident_impact_view - SEV1 only ===' AS demo;
SELECT incident_id, severity, component_name, impact_level,
       responsible_team, resolution_minutes
FROM incident_impact_view
WHERE severity = 'SEV1'
ORDER BY incident_id,
         FIELD(impact_level, 'RootCause', 'Unavailable', 'Degraded', 'Minor');

SELECT '=== VIEW 4/4: dependency_edge_view - the graph, readable ===' AS demo;
SELECT component_name, depends_on_name, dependency_type, is_critical
FROM dependency_edge_view
ORDER BY component_name, depends_on_name;


-- =====================================================================
-- PART 2 - STORED FUNCTIONS (6)
-- =====================================================================

SELECT '=== FUNCTIONS: all six, side by side ===' AS demo;
SELECT
    c.component_name,
    fn_direct_dependent_count(c.component_id) AS direct_dependents,
    fn_blast_radius_count(c.component_id)     AS blast_radius,
    fn_dependency_depth(c.component_id)       AS dependency_depth,
    fn_incident_count(c.component_id)         AS incidents,
    fn_risk_score(c.component_id)             AS risk_score
FROM component c
WHERE c.component_name IN
      ('Kafka Event Bus', 'Redis Cache', 'Payment DB', 'Order Service',
       'API Gateway', 'Shipping Service', 'Legacy Coupon Service')
ORDER BY risk_score DESC, c.component_name;

SELECT '=== fn_would_create_cycle: the guard behind full cycle prevention ===' AS demo;
SELECT
    src.component_name AS would_depend_on_this,
    tgt.component_name AS this_component,
    fn_would_create_cycle(src.component_id, tgt.component_id) AS creates_cycle,
    CASE fn_would_create_cycle(src.component_id, tgt.component_id)
         WHEN 1 THEN 'REJECTED - would close a loop'
         ELSE 'allowed'
    END AS verdict
FROM (SELECT 3 AS component_id UNION SELECT 4 UNION SELECT 11 UNION SELECT 6) ids
JOIN component src ON src.component_id = ids.component_id
JOIN component tgt ON tgt.component_id = 3      -- all tested against Order Service
ORDER BY creates_cycle DESC, src.component_name;


-- =====================================================================
-- PART 3 - STORED PROCEDURES (5)
-- =====================================================================

SELECT '=== PROCEDURE 1/5: sp_get_dependencies(3) - what Order Service NEEDS ===' AS demo;
CALL sp_get_dependencies(3);

SELECT '=== PROCEDURE 2/5: sp_get_blast_radius(11) - what breaks if Payment DB fails ===' AS demo;
CALL sp_get_blast_radius(11);

SELECT '=== PROCEDURE 2/5 again: sp_get_blast_radius(16) - Kafka, the worst case ===' AS demo;
CALL sp_get_blast_radius(16);

SELECT '=== PROCEDURE 3/5: sp_team_health_report(2) - the Payments Team ===' AS demo;
CALL sp_team_health_report(2);

SELECT '=== PROCEDURE 4/5: sp_component_impact_report(11) - FIVE result sets ===' AS demo;
CALL sp_component_impact_report(11);

SELECT '=== PROCEDURE 5/5: sp_detect_dependency_cycles() - expect NO ROWS ===' AS demo;
CALL sp_detect_dependency_cycles();
SELECT 'No table printed above = the dependency graph is acyclic.' AS interpretation;


-- =====================================================================
-- PART 4 - TRIGGERS THAT MAINTAIN DATA
--
-- Wrapped in a transaction and rolled back, so the dataset is unchanged.
-- =====================================================================

SELECT '=== TRIGGER: closing an incident stamps resolved_at automatically ===' AS demo;

START TRANSACTION;

SELECT incident_id, status, resolved_at AS before_update
FROM incident WHERE incident_id = 12;

UPDATE incident SET status = 'Resolved' WHERE incident_id = 12;

SELECT incident_id, status, resolved_at AS after_closing_it
FROM incident WHERE incident_id = 12;

UPDATE incident SET status = 'Open' WHERE incident_id = 12;

SELECT incident_id, status, resolved_at AS after_reopening_it
FROM incident WHERE incident_id = 12;

ROLLBACK;

SELECT 'Rolled back - incident 12 is untouched.' AS note;


-- =====================================================================
-- PART 5 - THE DATABASE REFUSING BAD DATA
--
-- The eight statements below are SUPPOSED to fail. Each error IS the
-- result. Run this file with --force so it continues past them.
-- =====================================================================

SELECT '=== The next EIGHT statements must all FAIL ===' AS demo;

-- 1. TRIGGER: a component cannot depend on itself
INSERT INTO dependency (component_id, depends_on_id) VALUES (3, 3);

-- 2. TRIGGER: direct mutual cycle. Order Service(3) -> Payment Service(4)
--    already exists, so 4 -> 3 would close a two-node loop.
INSERT INTO dependency (component_id, depends_on_id) VALUES (4, 3);

-- 3. TRIGGER: TRANSITIVE cycle, three nodes.
--    3 -> 4 -> 11 exists, so 11 -> 3 would close the loop.
--    This is the case the earlier implementation could NOT catch.
INSERT INTO dependency (component_id, depends_on_id) VALUES (11, 3);

-- 4. TRIGGER: transitive cycle, four nodes.
--    1 -> 3 -> 4 -> 11 exists, so 11 -> 1 would close it.
INSERT INTO dependency (component_id, depends_on_id) VALUES (11, 1);

-- 5. TRIGGER: a retired component cannot be deployed to production.
--    Legacy Coupon Service (10) has is_active = 0; environment 3 is Production.
INSERT INTO deployment (component_id, environment_id, version, deployed_at)
VALUES (10, 3, 'v1.0.0', '2027-01-01 10:00:00');

-- 6. TRIGGER on UPDATE: the same rule cannot be bypassed by editing a row.
UPDATE deployment SET component_id = 10, environment_id = 3 WHERE deployment_id = 35;

-- 7. CHECK: severity must be SEV1..SEV4
INSERT INTO incident (title, severity, status, environment_id, started_at)
VALUES ('invalid severity', 'SEV9', 'Open', 3, '2026-09-10 10:00:00');

-- 8. UNIQUE: the natural key blocks a duplicate deployment event
INSERT INTO deployment (component_id, environment_id, version, deployed_at)
VALUES (3, 3, 'v5.1.0', '2026-06-08 09:30:00');


SELECT '=== Demo complete. Every bad write was rejected; data is unchanged. ===' AS demo;

SELECT 'dependency'         AS table_name, COUNT(*) AS rows_now, 29 AS expected,
       IF(COUNT(*) = 29, 'PASS', 'FAIL') AS result FROM dependency
UNION ALL SELECT 'deployment', COUNT(*), 46, IF(COUNT(*) = 46, 'PASS', 'FAIL') FROM deployment
UNION ALL SELECT 'incident',   COUNT(*), 22, IF(COUNT(*) = 22, 'PASS', 'FAIL') FROM incident
UNION ALL SELECT 'component',  COUNT(*), 19, IF(COUNT(*) = 19, 'PASS', 'FAIL') FROM component;
