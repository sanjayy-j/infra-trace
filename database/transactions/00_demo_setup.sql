-- =====================================================================
-- InfraTrace - Transaction demo: setup
-- File   : database/transactions/00_demo_setup.sql
--
-- Creates a small set of clearly-marked DEMO rows (ids 9001+) that the
-- transaction and concurrency demonstrations operate on.
--
-- Why dedicated rows: the concurrency demos deliberately COMMIT changes,
-- overwrite values and cause deadlocks. Doing that to the seeded
-- ShopSphere dataset would corrupt the analytical results. These rows use
-- the REAL schema, with the REAL constraints and triggers, so nothing
-- about the demonstration is faked - only the rows are disposable.
--
-- Run:  cd database
--       mysql -u root -p infratrace < transactions/00_demo_setup.sql
--
-- Undo: transactions/99_demo_teardown.sql
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

-- Remove any leftovers from a previous run so this file is re-runnable.
DELETE FROM component WHERE component_id BETWEEN 9001 AND 9099;
DELETE FROM incident  WHERE incident_id  BETWEEN 9001 AND 9099;

-- A demo component owned by the Payments Team.
-- 'Checkout Coordinator' is the thing two engineers will fight over.
INSERT INTO component
    (component_id, component_name, component_type, owner_team_id, criticality, tech_stack, is_active, created_on)
VALUES
    (9001, 'DEMO Checkout Coordinator', 'Service', 2, 'High', 'Go', 1, '2026-01-10'),
    (9002, 'DEMO Coordinator DB',       'Database', 2, 'High', 'PostgreSQL 15', 1, '2026-01-10');

-- Give it a real place in the graph so the triggers are genuinely active.
INSERT INTO dependency (component_id, depends_on_id, dependency_type, is_critical, description)
VALUES (9001, 9002, 'Synchronous', 1, 'Demo coordinator state store.');

-- A production deployment history for the demo component.
INSERT INTO deployment
    (deployment_id, component_id, environment_id, version, deployed_by, deployed_at, status)
VALUES
    (9001, 9001, 2, 'v1.0.0', 3, '2026-09-01 10:00:00', 'Success'),
    (9002, 9001, 3, 'v1.0.0', 4, '2026-09-03 10:00:00', 'Success');

-- An OPEN incident on the demo component - two responders will both try
-- to update it at the same time.
INSERT INTO incident
    (incident_id, title, severity, status, environment_id, reported_by, started_at, root_cause)
VALUES
    (9001, 'DEMO Checkout coordinator latency spike', 'SEV2', 'Open', 3, 4,
     '2026-09-10 08:00:00', NULL);

INSERT INTO incident_component (incident_id, component_id, impact_level)
VALUES (9001, 9001, 'RootCause');

SELECT 'Demo rows created (component 9001/9002, deployment 9001/9002, incident 9001).' AS status;

SELECT component_id, component_name, criticality, is_active
FROM component WHERE component_id BETWEEN 9001 AND 9099;

SELECT incident_id, title, status, resolved_at
FROM incident WHERE incident_id BETWEEN 9001 AND 9099;
