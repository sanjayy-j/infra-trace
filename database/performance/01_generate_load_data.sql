-- =====================================================================
-- InfraTrace - Performance 1: build a high-volume dataset
-- File   : database/performance/01_generate_load_data.sql
--
-- WHY THIS FILE EXISTS
--
-- The ShopSphere dataset is 19 components and 29 dependency edges. At that
-- size the MySQL optimiser is right to ignore every index: reading 19 rows
-- sequentially is cheaper than descending a B-tree and then fetching rows.
-- So EXPLAIN on the real dataset reports "ALL" (full scan) almost
-- everywhere, and any "before/after adding an index" comparison would show
-- no difference at all.
--
-- Claiming an index sped something up when the plan never used it would be
-- dishonest. So this file builds a SEPARATE schema, infratrace_perf, with
-- the same structure at realistic enterprise scale:
--
--     ~2,000 components      ~12,000 dependency edges
--     ~40,000 deployments    ~6,000 incidents
--
-- At that size the optimiser's choices become visible and the index
-- comparisons in 02_explain_analysis.sql are real measurements.
--
-- The production schema `infratrace` is NEVER touched by this file.
--
-- Run:  cd database
--       mysql -u root -p < performance/01_generate_load_data.sql
--
-- Takes roughly 10-40 seconds. Uses a recursive CTE as a number generator,
-- so no external tooling or scripting language is needed.
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

DROP DATABASE IF EXISTS infratrace_perf;
CREATE DATABASE infratrace_perf CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE infratrace_perf;

-- Recursive CTEs default to a 1000-row limit; raise it for the generators.
SET SESSION cte_max_recursion_depth = 100000;

-- ---------------------------------------------------------------------
-- Structure: same columns and types as the real schema.
--
-- Deliberately created with NO secondary indexes. 02_explain_analysis.sql
-- adds them one at a time and measures the effect, which is only
-- meaningful if they start out absent.
-- ---------------------------------------------------------------------
CREATE TABLE team (
    team_id   INT AUTO_INCREMENT PRIMARY KEY,
    team_name VARCHAR(80) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE environment (
    environment_id   INT AUTO_INCREMENT PRIMARY KEY,
    environment_name VARCHAR(30) NOT NULL,
    is_production    TINYINT(1)  NOT NULL DEFAULT 0
) ENGINE=InnoDB;

CREATE TABLE component (
    component_id   INT AUTO_INCREMENT PRIMARY KEY,
    component_name VARCHAR(100) NOT NULL,
    component_type VARCHAR(20)  NOT NULL,
    owner_team_id  INT          NULL,
    criticality    VARCHAR(10)  NOT NULL,
    is_active      TINYINT(1)   NOT NULL DEFAULT 1,
    created_on     DATE         NOT NULL
) ENGINE=InnoDB;

CREATE TABLE dependency (
    dependency_id   INT AUTO_INCREMENT PRIMARY KEY,
    component_id    INT NOT NULL,
    depends_on_id   INT NOT NULL,
    dependency_type VARCHAR(20) NOT NULL,
    is_critical     TINYINT(1)  NOT NULL DEFAULT 0
) ENGINE=InnoDB;

CREATE TABLE deployment (
    deployment_id  INT AUTO_INCREMENT PRIMARY KEY,
    component_id   INT         NOT NULL,
    environment_id INT         NOT NULL,
    version        VARCHAR(30) NOT NULL,
    deployed_at    DATETIME    NOT NULL,
    status         VARCHAR(15) NOT NULL,
    is_rollback    TINYINT(1)  NOT NULL DEFAULT 0
) ENGINE=InnoDB;

CREATE TABLE incident (
    incident_id    INT AUTO_INCREMENT PRIMARY KEY,
    title          VARCHAR(150) NOT NULL,
    severity       VARCHAR(10)  NOT NULL,
    status         VARCHAR(15)  NOT NULL,
    environment_id INT          NOT NULL,
    started_at     DATETIME     NOT NULL,
    resolved_at    DATETIME     NULL
) ENGINE=InnoDB;

CREATE TABLE incident_component (
    incident_id  INT NOT NULL,
    component_id INT NOT NULL,
    impact_level VARCHAR(15) NOT NULL,
    PRIMARY KEY (incident_id, component_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------
INSERT INTO environment (environment_name, is_production) VALUES
('Development','0'), ('Staging','0'), ('Production','1');

INSERT INTO team (team_name)
WITH RECURSIVE n (i) AS (
    SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 40
)
SELECT CONCAT('Team ', LPAD(i, 3, '0')) FROM n;

-- ---------------------------------------------------------------------
-- 2,000 components
--
-- ~4% are left unowned and ~5% retired, matching the shape of the real
-- dataset so the ownership and production queries stay representative.
-- ---------------------------------------------------------------------
INSERT INTO component (component_name, component_type, owner_team_id, criticality, is_active, created_on)
WITH RECURSIVE n (i) AS (
    SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 2000
)
SELECT
    CONCAT(ELT(1 + (i % 10), 'Order','Payment','Inventory','Search','Auth',
                            'Notify','Recommend','Shipping','Billing','Catalogue'),
           ' ',
           ELT(1 + (i % 7), 'Service','Gateway','Worker','Store','Cache','Bus','API'),
           ' ', LPAD(i, 4, '0')),
    ELT(1 + (i % 7), 'Service','Database','Cache','Queue','Storage','Gateway','ExternalAPI'),
    CASE WHEN i % 25 = 0 THEN NULL ELSE 1 + (i % 40) END,
    ELT(1 + (i % 4), 'Low','Medium','High','Critical'),
    CASE WHEN i % 20 = 0 THEN 0 ELSE 1 END,
    DATE_SUB('2026-09-01', INTERVAL (i % 900) DAY)
FROM n;

-- ---------------------------------------------------------------------
-- ~12,000 dependency edges
--
-- Built so the graph stays ACYCLIC: an edge is only created from a HIGHER
-- component_id to a LOWER one. A strict ordering like that cannot contain
-- a cycle, which keeps the recursive traversals terminating and makes the
-- comparison against the real (also acyclic) graph fair.
--
-- The edge mix is chosen to imitate how real estates actually look, rather
-- than spraying edges uniformly:
--
--   * MOST edges are LOCAL - a component depends on something a little
--     below it in the ordering. That produces the layered structure real
--     systems have (gateway -> service -> service -> store) and gives the
--     recursive traversals genuine DEPTH to walk.
--
--   * A MINORITY of edges point into ids 1..30, the "shared platform"
--     components. Those become the heavily depended-on pieces - the
--     Kafka/Redis equivalents - which is what makes blast-radius ranking
--     interesting.
--
-- A uniform random lower id would instead give the first few components
-- over a thousand direct dependents each and everything else almost none,
-- which is not a shape any real infrastructure has.
-- ---------------------------------------------------------------------
INSERT INTO dependency (component_id, depends_on_id, dependency_type, is_critical)
WITH RECURSIVE
  n (i) AS (SELECT 31 UNION ALL SELECT i + 1 FROM n WHERE i < 2000),
  k (j) AS (SELECT 1 UNION ALL SELECT j + 1 FROM k WHERE j < 7)
SELECT DISTINCT
    n.i,
    CASE
        -- 2 of every 7 candidate edges go to a shared platform component
        WHEN k.j % 7 IN (0, 3)
            THEN 1 + ((n.i * 13 + k.j * 29) % 30)
        -- the rest stay local: somewhere in the 60 components below this one
        ELSE GREATEST(1, n.i - 1 - ((n.i * 17 + k.j * 41) % 60))
    END,
    ELT(1 + ((n.i + k.j) % 4), 'Synchronous','Asynchronous','Data','Config'),
    (n.i + k.j) % 2
FROM n JOIN k
WHERE (n.i * 3 + k.j * 5) % 7 < 4;         -- thins the edge set out

-- ---------------------------------------------------------------------
-- ~40,000 deployments, spread over three environments and ~2 years
-- ---------------------------------------------------------------------
INSERT INTO deployment (component_id, environment_id, version, deployed_at, status, is_rollback)
WITH RECURSIVE
  n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 2000),
  k (j) AS (SELECT 1 UNION ALL SELECT j + 1 FROM k WHERE j < 20)
SELECT
    n.i,
    1 + ((n.i + k.j) % 3),
    CONCAT('v', 1 + (k.j % 5), '.', (n.i % 10), '.', k.j),
    TIMESTAMPADD(HOUR, -((n.i * 17 + k.j * report_gap) % 17000), '2026-09-10 12:00:00'),
    CASE WHEN (n.i + k.j) % 17 = 0 THEN 'Failed' ELSE 'Success' END,
    CASE WHEN (n.i + k.j) % 31 = 0 THEN 1 ELSE 0 END
FROM n JOIN k CROSS JOIN (SELECT 7 AS report_gap) g;

-- ---------------------------------------------------------------------
-- ~6,000 incidents. Roughly a fifth remain unresolved, and SEV1 is rare -
-- the same skew a real incident history has.
-- ---------------------------------------------------------------------
INSERT INTO incident (title, severity, status, environment_id, started_at, resolved_at)
WITH RECURSIVE n (i) AS (
    SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 6000
)
SELECT
    CONCAT(ELT(1 + (i % 6), 'Latency spike','Connection errors','Timeout storm',
                            'Data staleness','Capacity exhaustion','Failover event'),
           ' #', i),
    -- SEV1 ~8%, SEV2 ~17%, SEV3 ~33%, SEV4 the rest
    CASE WHEN i % 12 = 0 THEN 'SEV1'
         WHEN i % 6  = 0 THEN 'SEV2'
         WHEN i % 3  = 0 THEN 'SEV3'
         ELSE 'SEV4' END,
    CASE WHEN i % 5 = 0 THEN 'Open' ELSE 'Resolved' END,
    1 + (i % 3),
    TIMESTAMPADD(HOUR, -((i * 3) % 17000), '2026-09-10 12:00:00'),
    CASE WHEN i % 5 = 0 THEN NULL
         ELSE TIMESTAMPADD(MINUTE, 30 + (i % 400),
              TIMESTAMPADD(HOUR, -((i * 3) % 17000), '2026-09-10 12:00:00'))
    END
FROM n;

-- ---------------------------------------------------------------------
-- ~18,000 incident-component links (about 3 components per incident)
-- ---------------------------------------------------------------------
INSERT INTO incident_component (incident_id, component_id, impact_level)
WITH RECURSIVE
  n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 6000),
  k (j) AS (SELECT 0 UNION ALL SELECT j + 1 FROM k WHERE j < 2)
SELECT DISTINCT
    n.i,
    1 + ((n.i * 11 + k.j * 97) % 2000),
    ELT(1 + k.j, 'RootCause','Degraded','Minor')
FROM n JOIN k;

-- ---------------------------------------------------------------------
-- Report what was built
-- ---------------------------------------------------------------------
ANALYZE TABLE component, dependency, deployment, incident, incident_component;

SELECT 'infratrace_perf built. Row counts:' AS status;
SELECT 'team' AS table_name, COUNT(*) AS rows_loaded FROM team
UNION ALL SELECT 'environment', COUNT(*) FROM environment
UNION ALL SELECT 'component', COUNT(*) FROM component
UNION ALL SELECT 'dependency', COUNT(*) FROM dependency
UNION ALL SELECT 'deployment', COUNT(*) FROM deployment
UNION ALL SELECT 'incident', COUNT(*) FROM incident
UNION ALL SELECT 'incident_component', COUNT(*) FROM incident_component;

-- Prove the generated graph is acyclic, so the recursive traversals in the
-- performance tests terminate for the same reason the real ones do.
SELECT 'generated graph is acyclic (edges always point to a lower id)' AS invariant,
       COUNT(*) AS upward_edges_should_be_zero,
       IF(COUNT(*) = 0, 'PASS', 'FAIL') AS result
FROM dependency WHERE depends_on_id >= component_id;

SELECT 'fan-in distribution (how shared the infrastructure is)' AS note;
SELECT dependents, COUNT(*) AS components_with_this_many_dependents
FROM (SELECT depends_on_id, COUNT(*) AS dependents FROM dependency GROUP BY depends_on_id) x
GROUP BY dependents ORDER BY dependents DESC LIMIT 10;
