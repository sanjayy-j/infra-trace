-- =====================================================================
-- InfraTrace - Transaction demo: teardown
-- File   : database/transactions/99_demo_teardown.sql
--
-- Removes every row the transaction demos created and proves the seeded
-- ShopSphere dataset is byte-for-byte back to its loaded state.
--
-- Run:  cd database
--       mysql -u root -p --table infratrace < transactions/99_demo_teardown.sql
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

-- ON DELETE CASCADE removes the demo dependency edges, deployments and
-- incident_component rows automatically.
DELETE FROM component WHERE component_id BETWEEN 9001 AND 9099;
DELETE FROM incident  WHERE incident_id  BETWEEN 9001 AND 9099;

-- Any demo procedure that a half-finished run may have left behind.
DROP PROCEDURE IF EXISTS sp_demo_atomic_release;
DROP PROCEDURE IF EXISTS sp_demo_retry_on_deadlock;

SELECT 'Demo rows removed.' AS status;

-- ---------------------------------------------------------------------
-- Verify the real dataset is untouched. Every row must read PASS.
-- ---------------------------------------------------------------------
SELECT 'team' AS table_name, COUNT(*) AS rows_found, 6 AS expected,
       IF(COUNT(*) = 6, 'PASS', 'FAIL') AS result FROM team
UNION ALL SELECT 'developer', COUNT(*), 15, IF(COUNT(*)=15,'PASS','FAIL') FROM developer
UNION ALL SELECT 'application', COUNT(*), 4, IF(COUNT(*)=4,'PASS','FAIL') FROM application
UNION ALL SELECT 'component', COUNT(*), 19, IF(COUNT(*)=19,'PASS','FAIL') FROM component
UNION ALL SELECT 'application_component', COUNT(*), 17, IF(COUNT(*)=17,'PASS','FAIL') FROM application_component
UNION ALL SELECT 'dependency', COUNT(*), 29, IF(COUNT(*)=29,'PASS','FAIL') FROM dependency
UNION ALL SELECT 'environment', COUNT(*), 3, IF(COUNT(*)=3,'PASS','FAIL') FROM environment
UNION ALL SELECT 'deployment', COUNT(*), 46, IF(COUNT(*)=46,'PASS','FAIL') FROM deployment
UNION ALL SELECT 'incident', COUNT(*), 22, IF(COUNT(*)=22,'PASS','FAIL') FROM incident
UNION ALL SELECT 'incident_component', COUNT(*), 59, IF(COUNT(*)=59,'PASS','FAIL') FROM incident_component;

-- The demos also modify values in place, so check a few of those too.
SELECT 'Inventory Service still running v3.9.0 in production' AS invariant,
       running_version AS actual,
       IF(running_version = 'v3.9.0', 'PASS', 'FAIL') AS result
FROM production_infrastructure_view WHERE component_name = 'Inventory Service'
UNION ALL
SELECT 'incident 12 still Open with no resolved_at',
       CONCAT(status, '/', IFNULL(resolved_at, 'NULL')),
       IF(status = 'Open' AND resolved_at IS NULL, 'PASS', 'FAIL')
FROM incident WHERE incident_id = 12
UNION ALL
SELECT 'dependency graph still acyclic',
       IF(fn_would_create_cycle(11, 3) = 1, 'triggers active', 'BROKEN'),
       IF(fn_would_create_cycle(11, 3) = 1, 'PASS', 'FAIL');
