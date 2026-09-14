-- =====================================================================
-- InfraTrace - Suite 01: schema objects exist
-- File   : tests/sql/01_schema.sql
-- Checks that every table, view, routine, trigger and index the project
-- claims to have is actually present in the database.
-- =====================================================================

USE infratrace;

-- --- object counts ---------------------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', 'base table count', '10', COUNT(*), IF(COUNT(*) = 10, 'PASS', 'FAIL')
FROM information_schema.tables
WHERE table_schema = 'infratrace' AND table_type = 'BASE TABLE';

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', 'view count', '4', COUNT(*), IF(COUNT(*) = 4, 'PASS', 'FAIL')
FROM information_schema.views WHERE table_schema = 'infratrace';

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', 'stored procedure count', '5', COUNT(*), IF(COUNT(*) = 5, 'PASS', 'FAIL')
FROM information_schema.routines
WHERE routine_schema = 'infratrace' AND routine_type = 'PROCEDURE';

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', 'stored function count', '6', COUNT(*), IF(COUNT(*) = 6, 'PASS', 'FAIL')
FROM information_schema.routines
WHERE routine_schema = 'infratrace' AND routine_type = 'FUNCTION';

INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', 'trigger count', '5', COUNT(*), IF(COUNT(*) = 5, 'PASS', 'FAIL')
FROM information_schema.triggers WHERE trigger_schema = 'infratrace';

-- --- every expected table by name ------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', CONCAT('table exists: ', t.name), '1',
       (SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = 'infratrace' AND table_name = t.name),
       IF((SELECT COUNT(*) FROM information_schema.tables
           WHERE table_schema = 'infratrace' AND table_name = t.name) = 1, 'PASS', 'FAIL')
FROM (
    SELECT 'team' AS name UNION ALL SELECT 'developer' UNION ALL SELECT 'application'
    UNION ALL SELECT 'component' UNION ALL SELECT 'application_component'
    UNION ALL SELECT 'dependency' UNION ALL SELECT 'environment'
    UNION ALL SELECT 'deployment' UNION ALL SELECT 'incident'
    UNION ALL SELECT 'incident_component'
) t;

-- --- every expected routine by name ----------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', CONCAT('routine exists: ', r.name), '1',
       (SELECT COUNT(*) FROM information_schema.routines
        WHERE routine_schema = 'infratrace' AND routine_name = r.name),
       IF((SELECT COUNT(*) FROM information_schema.routines
           WHERE routine_schema = 'infratrace' AND routine_name = r.name) = 1, 'PASS', 'FAIL')
FROM (
    SELECT 'fn_direct_dependent_count' AS name
    UNION ALL SELECT 'fn_blast_radius_count'  UNION ALL SELECT 'fn_incident_count'
    UNION ALL SELECT 'fn_would_create_cycle'  UNION ALL SELECT 'fn_dependency_depth'
    UNION ALL SELECT 'fn_risk_score'
    UNION ALL SELECT 'sp_get_dependencies'    UNION ALL SELECT 'sp_get_blast_radius'
    UNION ALL SELECT 'sp_team_health_report'  UNION ALL SELECT 'sp_component_impact_report'
    UNION ALL SELECT 'sp_detect_dependency_cycles'
) r;

-- --- every expected view by name -------------------------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', CONCAT('view exists: ', v.name), '1',
       (SELECT COUNT(*) FROM information_schema.views
        WHERE table_schema = 'infratrace' AND table_name = v.name),
       IF((SELECT COUNT(*) FROM information_schema.views
           WHERE table_schema = 'infratrace' AND table_name = v.name) = 1, 'PASS', 'FAIL')
FROM (
    SELECT 'component_ownership_view' AS name
    UNION ALL SELECT 'production_infrastructure_view'
    UNION ALL SELECT 'incident_impact_view'
    UNION ALL SELECT 'dependency_edge_view'
) v;

-- --- indexes that the analytical queries depend on --------------------
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', CONCAT('index exists: ', i.name), '1',
       (SELECT COUNT(DISTINCT index_name) FROM information_schema.statistics
        WHERE table_schema = 'infratrace' AND index_name = i.name),
       IF((SELECT COUNT(DISTINCT index_name) FROM information_schema.statistics
           WHERE table_schema = 'infratrace' AND index_name = i.name) = 1, 'PASS', 'FAIL')
FROM (
    SELECT 'idx_component_owner_type' AS name
    UNION ALL SELECT 'idx_dependency_reverse'
    UNION ALL SELECT 'idx_deployment_comp_env_time'
    UNION ALL SELECT 'idx_deployment_time'
    UNION ALL SELECT 'idx_incident_severity_status'
    UNION ALL SELECT 'idx_incident_started_at'
) i;

-- --- functions must NOT be declared DETERMINISTIC ---------------------
-- They read tables, so DETERMINISTIC would be a false declaration and is
-- unsafe with statement-based binary logging.
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'schema', 'no function falsely marked DETERMINISTIC', '0', COUNT(*),
       IF(COUNT(*) = 0, 'PASS', 'FAIL')
FROM information_schema.routines
WHERE routine_schema = 'infratrace' AND routine_type = 'FUNCTION'
  AND is_deterministic = 'YES';

SELECT 'Suite 01 (schema) complete.' AS status;
