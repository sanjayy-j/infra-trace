-- =====================================================================
-- InfraTrace - Test harness
-- File   : tests/sql/00_harness.sql
--
-- MySQL has no built-in assertion, so this file creates a temporary
-- results table that every later suite writes one row into. The final
-- summary in 99_summary.sql then reports totals and exits loudly if
-- anything failed.
--
-- The table is TEMPORARY, so it lives only for the duration of the one
-- client session that runs the suite, and leaves no trace in the schema.
-- That means the suites must be run in a SINGLE session - which is what
-- tests/run_all_tests.sql does.
--
-- Pattern used by every test:
--     INSERT INTO test_result (suite, test_name, expected, actual, result)
--     SELECT '<suite>', '<what is being checked>',
--            '<expected>', <actual expression>,
--            IF(<actual expression> = <expected>, 'PASS', 'FAIL')
--     FROM ...;
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

DROP TEMPORARY TABLE IF EXISTS test_result;

CREATE TEMPORARY TABLE test_result (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    suite     VARCHAR(40)  NOT NULL,
    test_name VARCHAR(140) NOT NULL,
    expected  VARCHAR(80)  NOT NULL,
    actual    VARCHAR(80)  NOT NULL,
    result    VARCHAR(4)   NOT NULL
);

SELECT 'Test harness ready.' AS status;
