-- =====================================================================
-- InfraTrace - Test summary
-- File   : tests/sql/99_summary.sql
-- Prints per-suite totals, then every failure in full, then one overall
-- verdict line. Read the verdict first.
-- =====================================================================

USE infratrace;

SELECT '================ PER-SUITE RESULTS ================' AS report;

SELECT
    suite,
    COUNT(*)                                        AS tests,
    SUM(result = 'PASS')                            AS passed,
    SUM(result = 'FAIL')                            AS failed,
    CONCAT(ROUND(100.0 * SUM(result = 'PASS') / COUNT(*), 1), '%') AS pass_rate
FROM test_result
GROUP BY suite
ORDER BY FIELD(suite, 'schema', 'constraints', 'integrity',
                      'functions', 'views', 'procedures', 'analytics', 'cycles');

SELECT '================ FAILURES (empty is good) ================' AS report;

SELECT suite, test_name, expected, actual
FROM test_result
WHERE result = 'FAIL'
ORDER BY id;

SELECT '================ VERDICT ================' AS report;

SELECT
    COUNT(*)             AS total_tests,
    SUM(result = 'PASS') AS passed,
    SUM(result = 'FAIL') AS failed,
    CASE WHEN SUM(result = 'FAIL') = 0
         THEN 'ALL TESTS PASSED'
         ELSE CONCAT('*** ', SUM(result = 'FAIL'), ' TEST(S) FAILED - see the failures above ***')
    END AS verdict
FROM test_result;
