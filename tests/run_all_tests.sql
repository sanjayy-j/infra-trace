-- =====================================================================
-- InfraTrace - Run the whole database test suite
-- File   : tests/run_all_tests.sql
--
-- The suites share a TEMPORARY results table, so they must all run in ONE
-- client session. SOURCE resolves paths relative to the directory the
-- mysql client was started in, so run this from the tests/ folder:
--
--     cd tests
--     mysql -u root -p --table < run_all_tests.sql
--
-- Read the VERDICT line at the very bottom of the output.
--
-- Safety: no test leaves data behind. Suite 02 attempts writes that the
-- database is expected to reject, and rolls back every transaction.
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

SOURCE sql/00_harness.sql;
SOURCE sql/01_schema.sql;
SOURCE sql/02_constraints.sql;
SOURCE sql/03_integrity.sql;
SOURCE sql/04_stored_programs.sql;
SOURCE sql/05_analytics.sql;
SOURCE sql/06_cycle_regression.sql;
SOURCE sql/99_summary.sql;
