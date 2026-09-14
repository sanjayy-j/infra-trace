-- =====================================================================
-- InfraTrace - Full database setup
-- File   : database/setup.sql
--
-- Builds the entire database from scratch, in dependency-safe order.
-- WARNING: this DROPs and recreates the `infratrace` database.
--
-- IMPORTANT: SOURCE resolves paths relative to the directory the mysql
-- client was STARTED in, so run this from inside the database/ folder:
--
--     cd database
--     mysql -u root -p
--     mysql> SOURCE setup.sql;
--
-- or in one line:
--
--     cd database
--     mysql -u root -p < setup.sql
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

SELECT '--- 1/6 creating schema ---' AS step;
SOURCE schema/schema.sql;

SELECT '--- 2/6 loading sample data ---' AS step;
SOURCE data/seed.sql;

SELECT '--- 3/6 creating views ---' AS step;
SOURCE views/views.sql;

SELECT '--- 4/6 creating functions ---' AS step;
SOURCE functions/functions.sql;

SELECT '--- 5/6 creating procedures ---' AS step;
SOURCE procedures/procedures.sql;

SELECT '--- 6/6 creating triggers ---' AS step;
SOURCE triggers/triggers.sql;

-- ---------------------------------------------------------------------
-- Final summary so a successful setup is obvious at a glance
-- ---------------------------------------------------------------------
USE infratrace;

SELECT 'InfraTrace database is ready.' AS status;

SELECT 'team'                  AS table_name, COUNT(*) AS row_count FROM team
UNION ALL SELECT 'developer',             COUNT(*) FROM developer
UNION ALL SELECT 'application',           COUNT(*) FROM application
UNION ALL SELECT 'component',             COUNT(*) FROM component
UNION ALL SELECT 'application_component', COUNT(*) FROM application_component
UNION ALL SELECT 'dependency',            COUNT(*) FROM dependency
UNION ALL SELECT 'environment',           COUNT(*) FROM environment
UNION ALL SELECT 'deployment',            COUNT(*) FROM deployment
UNION ALL SELECT 'incident',              COUNT(*) FROM incident
UNION ALL SELECT 'incident_component',    COUNT(*) FROM incident_component;
