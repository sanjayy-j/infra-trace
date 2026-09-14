-- =====================================================================
-- InfraTrace - Performance 2: EXPLAIN, indexes, and measured before/after
-- File   : database/performance/02_explain_analysis.sql
--
-- Runs against infratrace_perf (2,000 components / 7,878 dependency edges /
-- 40,000 deployments / 6,000 incidents), because at the real dataset's
-- 19 rows the optimiser correctly ignores every index and no comparison
-- would be visible.
--
-- Prerequisite: performance/01_generate_load_data.sql
-- Run:  cd database
--       mysql -u root -p --table < performance/02_explain_analysis.sql
--
-- For each of the seven access patterns the project actually depends on:
--   1. the query, and the business question behind it
--   2. EXPLAIN with no index            -> what the optimiser is forced to do
--   3. EXPLAIN ANALYZE with no index    -> what it actually costs
--   4. the index, with the reason it has that exact shape
--   5. EXPLAIN and EXPLAIN ANALYZE again -> the measured difference
--
-- HOW TO READ AN EXPLAIN
--   type=ALL     full table scan - every row examined
--   type=index   full scan of an index (better, still every entry)
--   type=range   an index range scan
--   type=ref     index lookup by a non-unique key   <- usually the goal
--   type=eq_ref  index lookup by a unique key
--   rows         the optimiser's ESTIMATE of rows examined (not exact)
--   key          the index actually chosen; NULL means none was usable
--   Extra        "Using index" = covering index, no row fetch needed
--                "Using filesort" / "Using temporary" = extra work
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

USE infratrace_perf;

-- ---------------------------------------------------------------------
-- Helper: MySQL 8 has no "DROP INDEX IF EXISTS" (that is PostgreSQL and
-- MariaDB syntax). This procedure provides the same convenience, so the
-- file can be re-run without first rebuilding the dataset.
-- ---------------------------------------------------------------------
DROP PROCEDURE IF EXISTS sp_drop_index_if_exists;
DELIMITER $$
CREATE PROCEDURE sp_drop_index_if_exists(IN p_table VARCHAR(64), IN p_index VARCHAR(64))
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.statistics
               WHERE table_schema = DATABASE()
                 AND table_name  = p_table
                 AND index_name  = p_index) THEN
        SET @ddl = CONCAT('DROP INDEX ', p_index, ' ON ', p_table);
        PREPARE st FROM @ddl;
        EXECUTE st;
        DEALLOCATE PREPARE st;
    END IF;
END$$
DELIMITER ;


SELECT '################ dataset size ################' AS section;
SELECT 'component' AS tbl, COUNT(*) AS rows_in_table FROM component
UNION ALL SELECT 'dependency', COUNT(*) FROM dependency
UNION ALL SELECT 'deployment', COUNT(*) FROM deployment
UNION ALL SELECT 'incident', COUNT(*) FROM incident
UNION ALL SELECT 'incident_component', COUNT(*) FROM incident_component;


-- =====================================================================
-- PATTERN 1 - REVERSE DEPENDENCY LOOKUP
--
-- Question: "We need to restart component 7. What depends on it?"
-- This is the single most important lookup in InfraTrace. It is also the
-- inner step of every level of the recursive blast-radius traversal, so
-- its cost is multiplied by the depth of the graph.
-- =====================================================================

SELECT '################ PATTERN 1: reverse dependency lookup ################' AS section;

CALL sp_drop_index_if_exists('dependency', 'idx_dependency_reverse');

SELECT '--- 1a. BEFORE: no index on depends_on_id ---' AS step;
EXPLAIN SELECT component_id FROM dependency WHERE depends_on_id = 7;

SELECT '--- 1b. BEFORE: measured ---' AS step;
EXPLAIN ANALYZE SELECT component_id FROM dependency WHERE depends_on_id = 7;

-- The index is (depends_on_id, component_id) and the column ORDER matters:
--   * depends_on_id must lead, because that is what the WHERE filters on.
--     An index led by component_id would be useless for this query.
--   * component_id second makes it a COVERING index: the answer is read
--     entirely from the index, with no lookup back into the table rows.
--     EXPLAIN shows this as "Using index".
CREATE INDEX idx_dependency_reverse ON dependency (depends_on_id, component_id);

SELECT '--- 1c. AFTER: with idx_dependency_reverse ---' AS step;
EXPLAIN SELECT component_id FROM dependency WHERE depends_on_id = 7;

SELECT '--- 1d. AFTER: measured ---' AS step;
EXPLAIN ANALYZE SELECT component_id FROM dependency WHERE depends_on_id = 7;


-- =====================================================================
-- PATTERN 2 - RECURSIVE BLAST-RADIUS TRAVERSAL
--
-- Question: "If component 7 fails, what could be affected?"
-- The recursive member re-runs the pattern-1 lookup at every level.
--
-- This pattern produced the most useful optimisation finding in the whole
-- project, and it is NOT about an index. It is about the SHAPE of the CTE.
--
-- MEASURED on this dataset (component 7 has 79 direct dependents):
--     CTE columns (component_id, depth)   54.5 ms, materialises 31,331 rows
--     CTE columns (component_id)           3.7 ms, materialises  1,942 rows
--                                          ~15x faster, 16x fewer rows
--
-- WHY: the recursive UNION deduplicates on the ENTIRE row, not on
-- component_id. With depth in the row, (component 500, depth 3) and
-- (component 500, depth 7) are DIFFERENT rows, so the same component is
-- expanded again for every distinct depth at which it can be reached. In a
-- graph with many alternative paths that becomes combinatorial path
-- exploration. Drop the depth column and each component is expanded
-- exactly once, which is the textbook transitive closure.
--
-- Consequence for this project, and it is already applied:
--   * fn_blast_radius_count() only needs a COUNT, so its CTE carries
--     component_id alone. That is a deliberate optimisation, not an
--     accident of style.
--   * sp_get_blast_radius() must report hops_away, so it has to carry
--     depth. Its output is correct (it takes MIN(depth) per component);
--     only its intermediate work is larger. At the real dataset's 29 edges
--     this costs microseconds. It is documented here so the trade-off is a
--     known decision rather than a surprise.
-- =====================================================================

SELECT '################ PATTERN 2: recursive blast radius ################' AS section;

SELECT '--- 2a. plan for the traversal (index in place) ---' AS step;
EXPLAIN
WITH RECURSIVE affected (component_id) AS (
    SELECT component_id FROM dependency WHERE depends_on_id = 7
    UNION
    SELECT d.component_id
    FROM dependency d JOIN affected a ON d.depends_on_id = a.component_id
)
SELECT COUNT(DISTINCT component_id) FROM affected;

SELECT '--- 2b. CTE carrying depth: measured ---' AS step;
EXPLAIN ANALYZE
WITH RECURSIVE affected (component_id, depth) AS (
    SELECT component_id, 1 FROM dependency WHERE depends_on_id = 7
    UNION
    SELECT d.component_id, a.depth + 1
    FROM dependency d JOIN affected a ON d.depends_on_id = a.component_id
    WHERE a.depth < 20
)
SELECT COUNT(DISTINCT component_id) FROM affected;

SELECT '--- 2c. CTE WITHOUT depth: measured (same answer, far less work) ---' AS step;
EXPLAIN ANALYZE
WITH RECURSIVE affected (component_id) AS (
    SELECT component_id FROM dependency WHERE depends_on_id = 7
    UNION
    SELECT d.component_id
    FROM dependency d JOIN affected a ON d.depends_on_id = a.component_id
)
SELECT COUNT(DISTINCT component_id) FROM affected;

SELECT '--- 2d. same traversal with NO index on depends_on_id ---' AS step;
-- Without the index the optimiser switches from a per-level index lookup to
-- a hash join with a full table scan at every level.
--     with idx_dependency_reverse     3.5 ms   (cost 244)
--     without it                      8.0 ms   (cost 772,038)
-- So the index is worth roughly 2.3x here. Note how wildly the cost
-- ESTIMATE overstates the gap (3,000x) compared with the measured 2.3x -
-- a reminder that optimiser cost is a planning currency, not a prediction
-- of milliseconds.
CALL sp_drop_index_if_exists('dependency', 'idx_dependency_reverse');
EXPLAIN ANALYZE
WITH RECURSIVE affected (component_id) AS (
    SELECT component_id FROM dependency WHERE depends_on_id = 7
    UNION
    SELECT d.component_id
    FROM dependency d JOIN affected a ON d.depends_on_id = a.component_id
)
SELECT COUNT(DISTINCT component_id) FROM affected;
CREATE INDEX idx_dependency_reverse ON dependency (depends_on_id, component_id);


-- =====================================================================
-- PATTERN 3 - LATEST DEPLOYMENT PER COMPONENT AND ENVIRONMENT
--
-- Question: "What version of component 500 is running in production?"
-- This is what production_infrastructure_view resolves for every
-- component, so it runs 2,000 times to build that view.
-- =====================================================================

SELECT '################ PATTERN 3: latest deployment lookup ################' AS section;

CALL sp_drop_index_if_exists('deployment', 'idx_deployment_comp_env_time');

SELECT '--- 3a. BEFORE ---' AS step;
EXPLAIN SELECT version, deployed_at FROM deployment
WHERE component_id = 500 AND environment_id = 3 AND status = 'Success'
ORDER BY deployed_at DESC LIMIT 1;

SELECT '--- 3b. BEFORE: measured ---' AS step;
EXPLAIN ANALYZE SELECT version, deployed_at FROM deployment
WHERE component_id = 500 AND environment_id = 3 AND status = 'Success'
ORDER BY deployed_at DESC LIMIT 1;

-- Column order follows the query shape exactly:
--   component_id, environment_id  - the two equality filters, so they lead
--   deployed_at DESC              - the sort, so the index supplies the
--                                   ordering and MySQL can stop at the
--                                   first row instead of sorting. This is
--                                   what removes "Using filesort".
-- DESC is explicit because MySQL 8 supports genuinely descending indexes.
CREATE INDEX idx_deployment_comp_env_time
    ON deployment (component_id, environment_id, deployed_at DESC);

SELECT '--- 3c. AFTER ---' AS step;
EXPLAIN SELECT version, deployed_at FROM deployment
WHERE component_id = 500 AND environment_id = 3 AND status = 'Success'
ORDER BY deployed_at DESC LIMIT 1;

SELECT '--- 3d. AFTER: measured ---' AS step;
EXPLAIN ANALYZE SELECT version, deployed_at FROM deployment
WHERE component_id = 500 AND environment_id = 3 AND status = 'Success'
ORDER BY deployed_at DESC LIMIT 1;


-- =====================================================================
-- PATTERN 4 - INCIDENT FILTERING BY SEVERITY AND STATUS
--
-- Question: "Show every SEV1 incident still open."
-- The dashboard's headline number. Selective on both columns.
-- =====================================================================

SELECT '################ PATTERN 4: incident severity/status filter ################' AS section;

CALL sp_drop_index_if_exists('incident', 'idx_incident_severity_status');

SELECT '--- 4a. BEFORE ---' AS step;
EXPLAIN SELECT incident_id, title, started_at FROM incident
WHERE severity = 'SEV1' AND status = 'Open';

SELECT '--- 4b. BEFORE: measured ---' AS step;
EXPLAIN ANALYZE SELECT incident_id, title, started_at FROM incident
WHERE severity = 'SEV1' AND status = 'Open';

-- severity leads because it is the more selective of the two here
-- (SEV1 is ~8% of rows, Open is ~20%). Putting the more selective column
-- first discards more rows earlier in the B-tree descent.
CREATE INDEX idx_incident_severity_status ON incident (severity, status);

SELECT '--- 4c. AFTER ---' AS step;
EXPLAIN SELECT incident_id, title, started_at FROM incident
WHERE severity = 'SEV1' AND status = 'Open';

SELECT '--- 4d. AFTER: measured ---' AS step;
EXPLAIN ANALYZE SELECT incident_id, title, started_at FROM incident
WHERE severity = 'SEV1' AND status = 'Open';


-- =====================================================================
-- PATTERN 5 - DEPLOY-THEN-INCIDENT CORRELATION
--
-- Question: "Which production deployments were followed by an incident on
-- the same component within 7 days?"
-- A four-table join with a time range - the most expensive analytical
-- query in the project.
--
-- This is the pattern where an index that LOOKS obviously right turns out
-- to be harmful, so it is worth walking through carefully.
--
-- The bridge table incident_component has PRIMARY KEY (incident_id,
-- component_id). This query traverses it from the COMPONENT side, which
-- that key cannot serve. The textbook move is therefore to add
-- incident_component(component_id, incident_id).
--
-- MEASURED, all four combinations:
--     no extra index                     39.2 ms   (optimiser cost 46,065)
--     + incident(started_at)             34.4 ms   (cost 46,065)
--     + incident_component(component_id) 108  ms   (cost  5,001)
--     + both                             111  ms   (cost  5,001)
--
-- The composite bridge index makes the optimiser's cost estimate NINE
-- TIMES better and the actual query nearly THREE TIMES SLOWER. Given the
-- new index the optimiser reorders the join to drive from deployment,
-- which means a full scan of all 40,000 deployment rows followed by about
-- 112,000 index lookups, instead of driving from the 6,000-row incident
-- table. The cost model simply gets this one wrong.
--
-- DECISION: keep incident(started_at), a small genuine win.
--           REJECT incident_component(component_id, incident_id).
--
-- This is the entire argument for measuring rather than adding indexes by
-- reflex. "It should help" and "EXPLAIN says it is cheaper" are both
-- weaker evidence than a stopwatch.
--
-- Footnote: in the REAL infratrace schema a single-column index on
-- incident_component(component_id) exists anyway, because InnoDB creates
-- one automatically for the foreign key. That is unavoidable and harmless
-- at the real data size; the finding above is about the composite index.
-- =====================================================================

SELECT '################ PATTERN 5: deploy-then-incident correlation ################' AS section;

CALL sp_drop_index_if_exists('incident', 'idx_incident_started_at');
CALL sp_drop_index_if_exists('incident_component', 'idx_inccomp_component');

SELECT '--- 5a. BEFORE: plan with no extra index ---' AS step;
EXPLAIN SELECT COUNT(*)
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN incident_component ic ON ic.component_id  = d.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
WHERE e.is_production = 1 AND d.status = 'Success'
  AND i.started_at > d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY;

SELECT '--- 5b. BEFORE: measured ---' AS step;
EXPLAIN ANALYZE SELECT COUNT(*)
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN incident_component ic ON ic.component_id  = d.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
WHERE e.is_production = 1 AND d.status = 'Success'
  AND i.started_at > d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY;

SELECT '--- 5c. with incident(started_at) only: the index we KEEP ---' AS step;
CREATE INDEX idx_incident_started_at ON incident (started_at);
EXPLAIN ANALYZE SELECT COUNT(*)
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN incident_component ic ON ic.component_id  = d.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
WHERE e.is_production = 1 AND d.status = 'Success'
  AND i.started_at > d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY;

SELECT '--- 5d. plus the bridge index: cost LOOKS better, runtime is WORSE ---' AS step;
CREATE INDEX idx_inccomp_component ON incident_component (component_id, incident_id);
EXPLAIN SELECT COUNT(*)
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN incident_component ic ON ic.component_id  = d.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
WHERE e.is_production = 1 AND d.status = 'Success'
  AND i.started_at > d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY;

EXPLAIN ANALYZE SELECT COUNT(*)
FROM deployment d
JOIN environment e         ON d.environment_id = e.environment_id
JOIN incident_component ic ON ic.component_id  = d.component_id
JOIN incident i            ON ic.incident_id   = i.incident_id
WHERE e.is_production = 1 AND d.status = 'Success'
  AND i.started_at > d.deployed_at
  AND i.started_at <= d.deployed_at + INTERVAL 7 DAY;

SELECT '--- 5e. so we DROP the harmful index and keep only started_at ---' AS step;
CALL sp_drop_index_if_exists('incident_component', 'idx_inccomp_component');
SELECT 'idx_inccomp_component rejected on measured evidence' AS decision;


-- =====================================================================
-- PATTERN 6 - OWNERSHIP AUDIT
--
-- Question: "What does team 7 own, broken down by type?" - and its sibling,
-- "which components have no owner at all?"
--
-- These two were the last indexes in the project without a measurement
-- behind them. An audit flagged that as inconsistent with this file's own
-- stated method ("keep the index only if the measurement supports it"), so
-- they are measured here rather than asserted.
-- =====================================================================

SELECT '################ PATTERN 6: ownership audit ################' AS section;

CALL sp_drop_index_if_exists('component', 'idx_component_owner_type');

SELECT '--- 6a. BEFORE ---' AS step;
EXPLAIN SELECT component_type, COUNT(*) FROM component
WHERE owner_team_id = 7 GROUP BY component_type;

SELECT '--- 6b. BEFORE: measured ---' AS step;
EXPLAIN ANALYZE SELECT component_type, COUNT(*) FROM component
WHERE owner_team_id = 7 GROUP BY component_type;

-- owner_team_id leads because it is the equality filter; component_type
-- second both satisfies the GROUP BY from the index order and makes the
-- index COVERING, so the query never touches the table rows.
CREATE INDEX idx_component_owner_type ON component (owner_team_id, component_type);

SELECT '--- 6c. AFTER ---' AS step;
EXPLAIN SELECT component_type, COUNT(*) FROM component
WHERE owner_team_id = 7 GROUP BY component_type;

SELECT '--- 6d. AFTER: measured ---' AS step;
EXPLAIN ANALYZE SELECT component_type, COUNT(*) FROM component
WHERE owner_team_id = 7 GROUP BY component_type;

-- The same index also serves the unowned-components audit through its
-- leading column alone, which is why it is composite rather than two
-- separate indexes.
SELECT '--- 6e. the unowned audit uses the leading column ---' AS step;
EXPLAIN ANALYZE SELECT COUNT(*) FROM component WHERE owner_team_id IS NULL;


-- =====================================================================
-- PATTERN 7 - DEPLOYMENT TIMELINE
--
-- Question: "when was the most recent deployment anywhere?" - which is how
-- the analytical queries anchor their notion of "recent" - and "how many
-- deployments happened in a given month?"
--
-- Note this is NOT the same access path as pattern 3. There the filter was
-- on a specific component and environment; here there is no such filter, so
-- idx_deployment_comp_env_time cannot seek and can only be scanned.
-- =====================================================================

SELECT '################ PATTERN 7: deployment timeline ################' AS section;

CALL sp_drop_index_if_exists('deployment', 'idx_deployment_time');

SELECT '--- 7a. BEFORE: MAX(deployed_at) ---' AS step;
EXPLAIN SELECT MAX(deployed_at) FROM deployment;

SELECT '--- 7b. BEFORE: measured ---' AS step;
EXPLAIN ANALYZE SELECT MAX(deployed_at) FROM deployment;
EXPLAIN ANALYZE SELECT COUNT(*) FROM deployment
WHERE deployed_at >= '2026-08-01' AND deployed_at < '2026-09-01';

CREATE INDEX idx_deployment_time ON deployment (deployed_at);

SELECT '--- 7c. AFTER ---' AS step;
-- Watch for "Select tables optimized away": with deployed_at as the leading
-- column of its own index, MAX() is answered by reading the LAST entry of the
-- B-tree. No rows are examined at all - the optimiser resolves the value
-- during planning.
EXPLAIN SELECT MAX(deployed_at) FROM deployment;

SELECT '--- 7d. AFTER: measured ---' AS step;
EXPLAIN ANALYZE SELECT MAX(deployed_at) FROM deployment;
EXPLAIN ANALYZE SELECT COUNT(*) FROM deployment
WHERE deployed_at >= '2026-08-01' AND deployed_at < '2026-09-01';


-- =====================================================================
-- COUNTER-EXAMPLE - an index the optimiser will REFUSE to use
--
-- Indexes are not free and not always used. Two rules worth showing,
-- because both bite in practice:
--
--   1. A leading wildcard LIKE '%x%' cannot use a B-tree index. The index
--      is ordered by prefix, and a pattern with no known prefix gives
--      nothing to seek to.
--   2. Wrapping an indexed column in a function hides it from the index.
--      YEAR(started_at) = 2026 cannot use an index on started_at; the
--      sargable rewrite started_at >= '2026-01-01' can.
-- =====================================================================

SELECT '################ COUNTER-EXAMPLE: when an index cannot help ################' AS section;

CALL sp_drop_index_if_exists('component', 'idx_component_name_tmp');
CREATE INDEX idx_component_name_tmp ON component (component_name);

-- Measured result: type=index, meaning MySQL scans the ENTIRE index rather
-- than seeking into it. It reads every one of the 2,000 entries. That is
-- marginally cheaper than a table scan only because the index is narrow and
-- covering - it is emphatically not an index LOOKUP.
SELECT '--- leading wildcard: no seek possible, full index scan (type=index) ---' AS step;
EXPLAIN SELECT component_id FROM component WHERE component_name LIKE '%Service%';

SELECT '--- prefix match on the SAME column: index USED (type=range) ---' AS step;
EXPLAIN SELECT component_id FROM component WHERE component_name LIKE 'Order%';

CALL sp_drop_index_if_exists('component', 'idx_component_name_tmp');

-- Same story: type=index (scan all 5,900 entries), not type=range.
SELECT '--- function on an indexed column: no seek possible (type=index) ---' AS step;
EXPLAIN SELECT COUNT(*) FROM incident WHERE YEAR(started_at) = 2026;

SELECT '--- the sargable rewrite: index USED ---' AS step;
EXPLAIN SELECT COUNT(*) FROM incident
WHERE started_at >= '2026-01-01' AND started_at < '2027-01-01';


-- =====================================================================
-- FINAL INDEX SET AND ITS COST
--
-- Indexes are a trade: faster reads, slower writes, more disk. Worth
-- stating the size explicitly rather than pretending indexes are free.
-- =====================================================================

SELECT '################ index set and storage cost ################' AS section;

ANALYZE TABLE component, dependency, deployment, incident, incident_component;

SELECT
    table_name,
    ROUND(data_length  / 1024 / 1024, 2) AS data_mb,
    ROUND(index_length / 1024 / 1024, 2) AS index_mb,
    ROUND(100.0 * index_length / GREATEST(data_length, 1), 1) AS index_pct_of_data
FROM information_schema.tables
WHERE table_schema = 'infratrace_perf' AND table_type = 'BASE TABLE'
ORDER BY index_length DESC;

SELECT table_name, index_name,
       GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns,
       MAX(cardinality) AS cardinality
FROM information_schema.statistics
WHERE table_schema = 'infratrace_perf'
GROUP BY table_name, index_name
ORDER BY table_name, index_name;

DROP PROCEDURE IF EXISTS sp_drop_index_if_exists;
