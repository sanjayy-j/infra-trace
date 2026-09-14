-- =====================================================================
-- InfraTrace - Sample dataset
-- File   : database/data/seed.sql
-- Company: ShopSphere (fictional e-commerce company)
-- Purpose: Realistic data so every analytical query returns useful rows.
--
-- Insertion order respects foreign keys:
--   team -> developer -> application -> component -> application_component
--   -> dependency -> environment -> deployment -> incident
--   -> incident_component
-- Primary keys are written explicitly so relationships stay readable.
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

SET FOREIGN_KEY_CHECKS = 1;

-- ---------------------------------------------------------------------
-- TEAMS (6)
-- ---------------------------------------------------------------------
INSERT INTO team (team_id, team_name, team_email, created_on) VALUES
(1, 'Platform Engineering', 'platform@shopsphere.io',   '2023-01-15'),
(2, 'Payments Team',        'payments@shopsphere.io',   '2023-02-01'),
(3, 'Commerce Team',        'commerce@shopsphere.io',   '2023-01-20'),
(4, 'Infrastructure Team',  'infra@shopsphere.io',      '2022-11-10'),
(5, 'Mobile Team',          'mobile@shopsphere.io',     '2023-06-05'),
(6, 'Data & Insights',      'data@shopsphere.io',       '2024-03-12');

-- ---------------------------------------------------------------------
-- DEVELOPERS (15)
-- ---------------------------------------------------------------------
INSERT INTO developer (developer_id, developer_name, email, job_role, team_id, joined_on) VALUES
(1,  'Aarav Menon',     'aarav.menon@shopsphere.io',     'Backend Engineer',       1, '2023-02-06'),
(2,  'Divya Rao',       'divya.rao@shopsphere.io',       'Engineering Manager',    1, '2023-01-16'),
(3,  'Kabir Shah',      'kabir.shah@shopsphere.io',      'Backend Engineer',       2, '2023-03-01'),
(4,  'Sneha Iyer',      'sneha.iyer@shopsphere.io',      'Site Reliability Engineer', 2, '2023-04-17'),
(5,  'Rohan Gupta',     'rohan.gupta@shopsphere.io',     'Backend Engineer',       3, '2023-02-20'),
(6,  'Meera Nair',      'meera.nair@shopsphere.io',      'Backend Engineer',       3, '2023-08-14'),
(7,  'Arjun Verma',     'arjun.verma@shopsphere.io',     'Engineering Manager',    3, '2023-01-23'),
(8,  'Priya Desai',     'priya.desai@shopsphere.io',     'Platform Engineer',      4, '2022-11-21'),
(9,  'Vikram Singh',    'vikram.singh@shopsphere.io',    'Site Reliability Engineer', 4, '2022-12-05'),
(10, 'Ananya Bose',     'ananya.bose@shopsphere.io',     'Infrastructure Engineer', 4, '2023-05-08'),
(11, 'Farhan Khan',     'farhan.khan@shopsphere.io',     'Mobile Engineer',        5, '2023-06-12'),
(12, 'Nisha Pillai',    'nisha.pillai@shopsphere.io',    'Mobile Engineer',        5, '2023-09-04'),
(13, 'Rahul Joshi',     'rahul.joshi@shopsphere.io',     'Data Engineer',          6, '2024-03-18'),
(14, 'Tanvi Kulkarni',  'tanvi.kulkarni@shopsphere.io',  'Data Engineer',          6, '2024-07-01'),
(15, 'Imran Sheikh',    'imran.sheikh@shopsphere.io',    'Site Reliability Engineer', 1, '2023-10-09');

-- ---------------------------------------------------------------------
-- APPLICATIONS (4)
-- ---------------------------------------------------------------------
INSERT INTO application (application_id, application_name, app_type, owner_team_id, description) VALUES
(1, 'ShopSphere Web',         'Web',      3, 'Customer-facing storefront and checkout website.'),
(2, 'ShopSphere Mobile',      'Mobile',   5, 'iOS and Android shopping application.'),
(3, 'ShopSphere Admin',       'Internal', 1, 'Internal console for catalogue, orders and refunds.'),
(4, 'ShopSphere Partner API', 'API',      1, 'Public API used by logistics and marketplace partners.');

-- ---------------------------------------------------------------------
-- COMPONENTS (19)
-- Components 9 and 10 have NO owning team on purpose - InfraTrace is
-- meant to surface unowned infrastructure. Component 10 is retired
-- (is_active = 0) but still has a live data dependency.
-- ---------------------------------------------------------------------
INSERT INTO component (component_id, component_name, component_type, owner_team_id, criticality, tech_stack, is_active, created_on) VALUES
(1,  'API Gateway',            'Gateway',     1,    'Critical', 'Kong / NGINX',        1, '2023-02-10'),
(2,  'Authentication Service', 'Service',     1,    'Critical', 'Java Spring Boot',    1, '2023-02-14'),
(3,  'Order Service',          'Service',     3,    'Critical', 'Java Spring Boot',    1, '2023-03-02'),
(4,  'Payment Service',        'Service',     2,    'Critical', 'Go',                  1, '2023-03-20'),
(5,  'Inventory Service',      'Service',     3,    'High',     'Java Spring Boot',    1, '2023-04-05'),
(6,  'Notification Service',   'Service',     1,    'Medium',   'Node.js',             1, '2023-05-11'),
(7,  'Recommendation Service', 'Service',     6,    'Medium',   'Python FastAPI',      1, '2024-04-02'),
(8,  'Search Service',         'Service',     3,    'High',     'Java Spring Boot',    1, '2023-07-19'),
(9,  'Shipping Service',       'Service',     NULL, 'Medium',   'Node.js',             1, '2023-09-27'),
(10, 'Legacy Coupon Service',  'Service',     NULL, 'Low',      'PHP 7.4',             0, '2023-01-30'),
(11, 'Payment DB',             'Database',    2,    'Critical', 'PostgreSQL 15',       1, '2023-03-18'),
(12, 'Orders DB',              'Database',    3,    'Critical', 'PostgreSQL 15',       1, '2023-03-01'),
(13, 'Inventory DB',           'Database',    3,    'High',     'PostgreSQL 15',       1, '2023-04-04'),
(14, 'User DB',                'Database',    1,    'Critical', 'PostgreSQL 15',       1, '2023-02-12'),
(15, 'Redis Cache',            'Cache',       4,    'High',     'Redis 7',             1, '2023-02-22'),
(16, 'Kafka Event Bus',        'Queue',       4,    'Critical', 'Apache Kafka 3.6',    1, '2023-02-25'),
(17, 'Object Storage',         'Storage',     4,    'Medium',   'S3-compatible store', 1, '2023-03-08'),
(18, 'Elasticsearch Cluster',  'Storage',     4,    'High',     'Elasticsearch 8',     1, '2023-07-15'),
(19, 'Stripe Payment API',     'ExternalAPI', 2,    'High',     'Third-party REST',    1, '2023-03-21');

-- ---------------------------------------------------------------------
-- APPLICATION <-> COMPONENT (17 links)
-- ---------------------------------------------------------------------
INSERT INTO application_component (application_id, component_id, usage_notes) VALUES
-- ShopSphere Web
(1, 1,  'All browser traffic enters through the gateway.'),
(1, 3,  'Cart and checkout.'),
(1, 4,  'Card and wallet payments.'),
(1, 7,  'Home page product recommendations.'),
(1, 8,  'Catalogue search bar.'),
(1, 6,  'Order confirmation emails.'),
-- ShopSphere Mobile
(2, 1,  'Mobile API traffic.'),
(2, 3,  'In-app checkout.'),
(2, 8,  'In-app search.'),
(2, 6,  'Push notifications.'),
(2, 2,  'Login and session refresh.'),
-- ShopSphere Admin
(3, 1,  'Admin console traffic.'),
(3, 2,  'Staff single sign-on.'),
(3, 5,  'Stock corrections.'),
(3, 3,  'Refunds and order lookup.'),
-- ShopSphere Partner API
(4, 1,  'Partner traffic and rate limiting.'),
(4, 9,  'Shipment tracking callbacks.');

-- ---------------------------------------------------------------------
-- DEPENDENCY GRAPH (29 edges)
-- Read as: component_id DEPENDS ON depends_on_id
-- ---------------------------------------------------------------------
INSERT INTO dependency (component_id, depends_on_id, dependency_type, is_critical, description) VALUES
-- API Gateway routes to the public-facing services
(1,  2,  'Synchronous',  1, 'Validates every request token.'),
(1,  3,  'Synchronous',  1, 'Routes checkout traffic.'),
(1,  8,  'Synchronous',  0, 'Routes search traffic.'),
(1,  7,  'Synchronous',  0, 'Routes recommendation widgets.'),
-- Authentication Service
(2,  14, 'Synchronous',  1, 'Stores users, roles and credentials.'),
(2,  15, 'Synchronous',  0, 'Caches session tokens.'),
(2,  16, 'Asynchronous', 0, 'Publishes login audit events.'),
-- Order Service
(3,  4,  'Synchronous',  1, 'Authorises payment at checkout.'),
(3,  5,  'Synchronous',  1, 'Reserves stock at checkout.'),
(3,  12, 'Synchronous',  1, 'Persists orders and order lines.'),
(3,  16, 'Asynchronous', 0, 'Publishes order-placed events.'),
(3,  9,  'Synchronous',  0, 'Requests shipment creation.'),
-- Payment Service
(4,  11, 'Synchronous',  1, 'Stores transactions and refunds.'),
(4,  15, 'Synchronous',  0, 'Idempotency key cache.'),
(4,  19, 'Synchronous',  1, 'External card authorisation.'),
(4,  16, 'Asynchronous', 0, 'Publishes payment-settled events.'),
-- Inventory Service
(5,  13, 'Synchronous',  1, 'Stock levels per warehouse.'),
(5,  15, 'Synchronous',  0, 'Hot product stock cache.'),
-- Notification Service
(6,  16, 'Asynchronous', 1, 'Consumes order and payment events.'),
(6,  17, 'Data',         0, 'Stores rendered email templates.'),
-- Recommendation Service
(7,  16, 'Asynchronous', 0, 'Consumes click-stream events.'),
(7,  17, 'Data',         0, 'Reads trained model artefacts.'),
(7,  13, 'Data',         0, 'Nightly catalogue snapshot read.'),
-- Search Service
(8,  18, 'Synchronous',  1, 'Serves the product search index.'),
(8,  13, 'Data',         0, 'Reindex source for catalogue data.'),
(8,  15, 'Synchronous',  0, 'Caches popular search results.'),
-- Shipping Service
(9,  12, 'Synchronous',  1, 'Reads orders to create shipments.'),
(9,  16, 'Asynchronous', 0, 'Publishes shipment status events.'),
-- Legacy Coupon Service (retired but still reads the catalogue database)
(10, 13, 'Data',         0, 'Legacy nightly coupon-eligibility job.');

-- ---------------------------------------------------------------------
-- ENVIRONMENTS (3)
-- ---------------------------------------------------------------------
INSERT INTO environment (environment_id, environment_name, region, is_production) VALUES
(1, 'Development', 'local-dev',  0),
(2, 'Staging',     'ap-south-1', 0),
(3, 'Production',  'ap-south-1', 1);

-- ---------------------------------------------------------------------
-- DEPLOYMENTS (46)
-- Several production deployments deliberately sit a few days before an
-- incident on the same component, so "deploy then incident" analysis
-- returns real correlations.
-- ---------------------------------------------------------------------
INSERT INTO deployment (deployment_id, component_id, environment_id, version, deployed_by, deployed_at, status) VALUES
(1,  3,  1, 'v5.1.0',     5,  '2026-06-02 10:15:00', 'Success'),
(2,  3,  2, 'v5.1.0',     5,  '2026-06-04 11:00:00', 'Success'),
(3,  3,  3, 'v5.1.0',     7,  '2026-06-08 09:30:00', 'Success'),
(4,  4,  1, 'v3.3.0',     3,  '2026-06-10 14:20:00', 'Success'),
(5,  4,  2, 'v3.3.0',     3,  '2026-06-12 15:05:00', 'Success'),
(6,  4,  3, 'v3.3.0',     4,  '2026-06-16 10:45:00', 'Success'),
(7,  2,  2, 'v2.7.1',     1,  '2026-06-20 13:10:00', 'Success'),
(8,  2,  3, 'v2.7.1',     15, '2026-06-24 09:00:00', 'Success'),
(9,  5,  2, 'v3.9.0',     6,  '2026-06-27 16:40:00', 'Success'),
(10, 5,  3, 'v3.9.0',     7,  '2026-07-01 10:05:00', 'Success'),
(11, 4,  2, 'v3.4.0',     3,  '2026-07-09 11:30:00', 'Success'),
(12, 4,  3, 'v3.4.0',     4,  '2026-07-12 18:20:00', 'Success'),  -- precedes incident 1
(13, 11, 3, 'pg15.4-p2',  9,  '2026-07-13 02:00:00', 'Success'),  -- precedes incident 1
(14, 16, 2, 'v3.6.1',     10, '2026-07-22 12:00:00', 'Success'),
(15, 16, 3, 'v3.6.1',     10, '2026-07-25 01:30:00', 'Success'),  -- precedes incident 2
(16, 15, 2, 'v7.2.4',     8,  '2026-07-29 14:45:00', 'Success'),
(17, 6,  3, 'v1.8.3',     1,  '2026-07-30 09:15:00', 'Success'),
(18, 15, 3, 'v7.2.4',     8,  '2026-08-01 03:20:00', 'Success'),  -- precedes incident 3
(19, 8,  2, 'v2.1.0',     6,  '2026-08-05 10:00:00', 'Success'),
(20, 18, 3, 'es8.11.2',   9,  '2026-08-06 22:10:00', 'Success'),  -- precedes incident 4
(21, 8,  3, 'v2.1.0',     6,  '2026-08-07 09:40:00', 'Success'),  -- precedes incident 4
(22, 5,  2, 'v4.0.0',     6,  '2026-08-11 15:25:00', 'Success'),
(23, 5,  3, 'v4.0.0',     7,  '2026-08-13 10:50:00', 'Success'),  -- precedes incident 5
(24, 5,  3, 'v3.9.0',     7,  '2026-08-15 15:35:00', 'Success'),   -- rollback after incident 5
(25, 19, 3, '2026-08-01', 3,  '2026-08-18 12:00:00', 'Success'),  -- precedes incident 6
(26, 7,  1, 'v0.9.0',     13, '2026-08-18 11:10:00', 'Success'),
(27, 17, 3, 'v2026.08.1', 10, '2026-08-20 08:00:00', 'Success'),  -- precedes incident 7
(28, 14, 3, 'pg15.4-p2',  9,  '2026-08-25 01:45:00', 'Success'),  -- precedes incident 8
(29, 2,  3, 'v2.8.0',     15, '2026-08-26 09:30:00', 'Success'),  -- precedes incident 8
(30, 7,  2, 'v0.9.0',     14, '2026-08-29 17:05:00', 'Success'),  -- precedes incident 9 (staging)
(31, 12, 3, 'pg15.4-p2',  9,  '2026-08-29 02:15:00', 'Success'),
(32, 13, 3, 'pg15.4-p2',  9,  '2026-08-31 02:15:00', 'Success'),
(33, 1,  2, 'v1.9.2',     8,  '2026-09-02 10:20:00', 'Success'),
(34, 1,  3, 'v1.9.2',     8,  '2026-09-04 08:15:00', 'Success'),  -- precedes incident 11
(35, 9,  2, 'v1.2.0',     1,  '2026-09-05 13:00:00', 'Failed'),
(36, 15, 3, 'v7.2.5',     8,  '2026-09-07 02:40:00', 'Success'),  -- precedes incident 12
(37, 9,  2, 'v1.2.1',     1,  '2026-09-08 13:45:00', 'Success'),
(38, 6,  1, 'v1.9.0',     1,  '2026-09-09 11:20:00', 'Success'),

-- Earlier history (Feb-May 2026). Included so that month-over-month trend
-- analysis and the window-function queries have more than one quarter of
-- data to work with, and so the older incidents below have a plausible
-- change that preceded them.
(39, 2,  3, 'v2.5.0',     15, '2026-02-11 09:40:00', 'Success'),
(40, 3,  3, 'v4.8.0',     7,  '2026-02-24 10:15:00', 'Success'),
(41, 16, 3, 'v3.5.0',     10, '2026-03-09 02:20:00', 'Success'),
(42, 12, 3, 'pg15.2-p1',  9,  '2026-03-21 01:30:00', 'Success'),
(43, 4,  3, 'v3.1.0',     4,  '2026-04-07 17:50:00', 'Success'),
(44, 18, 3, 'es8.9.0',    9,  '2026-04-19 22:40:00', 'Success'),
(45, 5,  3, 'v3.7.0',     6,  '2026-05-06 11:25:00', 'Success'),
(46, 15, 3, 'v7.1.0',     8,  '2026-05-22 03:10:00', 'Success');

-- Deployment 24 put the previous version (v3.9.0) back after incident 5.
-- It SUCCEEDED as a deployment, so status = 'Success'; what makes it special
-- is is_rollback = 1. Every other row keeps the column DEFAULT of 0.
-- This is why production_infrastructure_view correctly reports Inventory
-- Service as running v3.9.0 rather than the v4.0.0 that was rolled back.
UPDATE deployment SET is_rollback = 1 WHERE deployment_id = 24;

-- ---------------------------------------------------------------------
-- INCIDENTS (22)
-- ---------------------------------------------------------------------
INSERT INTO incident (incident_id, title, severity, status, environment_id, reported_by, started_at, resolved_at, root_cause, caused_by_deployment_id) VALUES
(1,  'Checkout failures caused by Payment DB connection pool exhaustion',
     'SEV1', 'Resolved',      3, 4,  '2026-07-14 09:20:00', '2026-07-14 12:05:00',
     'Payment Service v3.4.0 halved the pool size; all connections were held during a traffic spike.', 12),
(2,  'Kafka broker outage delayed order and payment events',
     'SEV1', 'Resolved',      3, 9,  '2026-07-28 18:40:00', '2026-07-28 21:15:00',
     'Two of three brokers restarted during the v3.6.1 rollout, leaving partitions without a leader.', 15),
(3,  'Redis eviction storm increased login latency',
     'SEV2', 'Resolved',      3, 15, '2026-08-03 07:10:00', '2026-08-03 09:45:00',
     'Max-memory policy changed to allkeys-lru, evicting live session tokens.', 18),
(4,  'Stale search results after a failed Elasticsearch reindex',
     'SEV2', 'Resolved',      3, 6,  '2026-08-09 14:00:00', '2026-08-09 17:30:00',
     'Reindex job ran out of disk on two data nodes and left the alias pointing at the old index.', NULL),
(5,  'Inventory oversell during the weekend flash sale',
     'SEV1', 'Resolved',      3, 5,  '2026-08-15 11:05:00', '2026-08-15 15:20:00',
     'Stock reservation in v4.0.0 was not transactional, so concurrent checkouts double-booked stock.', 23),
(6,  'Stripe API timeouts caused duplicate payment retries',
     'SEV2', 'Resolved',      3, 3,  '2026-08-19 20:15:00', '2026-08-19 22:00:00',
     'Upstream provider latency exceeded the client timeout and the retry policy had no backoff.', NULL),
(7,  'Notification emails delayed by object storage throttling',
     'SEV3', 'Resolved',      3, 1,  '2026-08-22 06:30:00', '2026-08-22 08:10:00',
     'Template bucket hit the request rate limit during a bulk campaign send.', NULL),
(8,  'Authentication errors after User DB failover',
     'SEV1', 'Resolved',      3, 2,  '2026-08-27 03:05:00', '2026-08-27 05:40:00',
     'Primary failed over to a replica that had not finished applying WAL segments.', NULL),
(9,  'Recommendation pipeline broken in staging after model rollout',
     'SEV3', 'Resolved',      2, 13, '2026-08-30 10:00:00', '2026-08-30 13:25:00',
     'Model artefact schema changed without a matching consumer update.', 30),
(10, 'Payment DB replica lag produced duplicate refunds',
     'SEV2', 'Mitigated',     3, 4,  '2026-09-02 16:45:00', NULL,
     'Refund status read from a lagging replica; reads moved to the primary as a temporary fix.', NULL),
(11, 'API Gateway rate limiter misconfiguration rejected valid traffic',
     'SEV2', 'Investigating', 3, 8,  '2026-09-06 12:30:00', NULL,
     'Under investigation: v1.9.2 applied the partner rate limit to logged-in customers.', NULL),
(12, 'Redis cluster node failure degraded checkout',
     'SEV1', 'Open',          3, 9,  '2026-09-09 19:10:00', NULL,
     'Under investigation: one cache node is unreachable and clients are not failing over cleanly.', NULL),
(13, 'Slow report generation in the admin console',
     'SEV4', 'Resolved',      3, 1,  '2026-09-01 09:00:00', '2026-09-01 11:30:00',
     'Missing index on the order-date column made the monthly report do a full scan.', NULL),

-- Earlier incident history (Feb-Jun 2026). All resolved, because they are
-- historical - the three unresolved incidents stay 10, 11 and 12. This gives
-- the trend and window-function queries eight months of history instead of
-- three, and it makes the repeat-offender analysis meaningful over time.
(14, 'Login failures after an auth service regression',
     'SEV2', 'Resolved',      3, 15, '2026-02-13 08:30:00', '2026-02-13 10:55:00',
     'A token-cache key change in v2.5.0 invalidated every active session.', 39),
(15, 'Order confirmation emails silently dropped',
     'SEV3', 'Resolved',      3, 1,  '2026-02-27 14:20:00', '2026-02-27 16:05:00',
     'Consumer group rebalanced and the new consumer never subscribed to the topic.', NULL),
(16, 'Event backlog after a Kafka partition reassignment',
     'SEV2', 'Resolved',      3, 9,  '2026-03-11 19:15:00', '2026-03-11 23:40:00',
     'Partition reassignment during v3.5.0 left consumers behind by four hours.', 41),
(17, 'Order history queries timing out',
     'SEV2', 'Resolved',      3, 5,  '2026-03-23 12:05:00', '2026-03-23 15:10:00',
     'A query plan regression after the pg15.2 patch made order lookups do a full scan.', 42),
(18, 'Duplicate charges on retried payments',
     'SEV1', 'Resolved',      3, 3,  '2026-04-09 16:40:00', '2026-04-09 20:25:00',
     'Idempotency keys were not persisted before calling the provider in v3.1.0.', 43),
(19, 'Search index corruption after a node restart',
     'SEV2', 'Resolved',      3, 6,  '2026-04-21 09:55:00', '2026-04-21 14:30:00',
     'A shard failed to recover cleanly and served a partial index for four hours.', 44),
(20, 'Stock counts drifting between warehouses',
     'SEV3', 'Resolved',      3, 6,  '2026-05-08 10:30:00', '2026-05-08 13:15:00',
     'A rounding change in the v3.7.0 reservation logic under-counted partial units.', 45),
(21, 'Session churn after a cache resize',
     'SEV3', 'Resolved',      3, 8,  '2026-05-24 07:45:00', '2026-05-24 09:20:00',
     'Resizing the cache in v7.1.0 dropped the keyspace and forced every user to log in again.', 46),
(22, 'Partner API returning stale shipment status',
     'SEV4', 'Resolved',      2, 11, '2026-06-18 11:00:00', '2026-06-18 13:40:00',
     'Shipment status cache was never invalidated on the staging environment.', NULL);

-- ---------------------------------------------------------------------
-- INCIDENT <-> COMPONENT (59 links)
-- 'RootCause' marks where the failure originated; the other rows record
-- components that were affected through the dependency chain.
--
-- Note on Inventory DB: incidents 5 and 20 were both application-logic faults
-- in Inventory Service - a non-transactional stock reservation, and a rounding
-- error in partial units. The database served every query correctly throughout,
-- so it is deliberately NOT listed as affected. That leaves Inventory DB with
-- no incident history at all, which is realistic for a well-run database and
-- is exactly the case query A4 in 08_advanced_sql.sql is written to surface:
-- a component with a WIDE blast radius (4 direct dependents) and no incidents,
-- which is either genuinely solid or under-monitored.
-- ---------------------------------------------------------------------
INSERT INTO incident_component (incident_id, component_id, impact_level) VALUES
-- 1: Payment DB connection pool
(1, 11, 'RootCause'), (1, 4, 'Unavailable'), (1, 3, 'Degraded'), (1, 1, 'Degraded'),
-- 2: Kafka outage
(2, 16, 'RootCause'), (2, 6, 'Unavailable'), (2, 7, 'Degraded'), (2, 3, 'Degraded'),
-- 3: Redis eviction storm
(3, 15, 'RootCause'), (3, 2, 'Degraded'), (3, 5, 'Degraded'),
-- 4: Elasticsearch reindex
(4, 18, 'RootCause'), (4, 8, 'Degraded'),
-- 5: Inventory oversell
(5, 5, 'RootCause'), (5, 3, 'Degraded'),
-- 6: Stripe timeouts
(6, 19, 'RootCause'), (6, 4, 'Degraded'),
-- 7: Object storage throttling
(7, 17, 'RootCause'), (7, 6, 'Degraded'),
-- 8: User DB failover
(8, 14, 'RootCause'), (8, 2, 'Unavailable'), (8, 1, 'Degraded'),
-- 9: Recommendation pipeline (staging)
(9, 7, 'RootCause'), (9, 16, 'Minor'),
-- 10: Payment DB replica lag
(10, 11, 'RootCause'), (10, 4, 'Degraded'),
-- 11: API Gateway rate limiter
(11, 1, 'RootCause'), (11, 3, 'Degraded'), (11, 8, 'Degraded'),
-- 12: Redis node failure
(12, 15, 'RootCause'), (12, 4, 'Degraded'), (12, 5, 'Degraded'), (12, 2, 'Minor'),
-- 13: Slow admin reports
(13, 12, 'RootCause'), (13, 3, 'Minor'),

-- Earlier history (incidents 14-22)
-- 14: auth regression
(14, 2, 'RootCause'), (14, 1, 'Degraded'), (14, 15, 'Minor'),
-- 15: dropped notification emails
(15, 6, 'RootCause'), (15, 16, 'Degraded'),
-- 16: Kafka partition reassignment
(16, 16, 'RootCause'), (16, 6, 'Degraded'), (16, 7, 'Degraded'), (16, 3, 'Minor'),
-- 17: order history timeouts
(17, 12, 'RootCause'), (17, 3, 'Degraded'), (17, 9, 'Minor'),
-- 18: duplicate charges
(18, 4, 'RootCause'), (18, 11, 'Degraded'), (18, 19, 'Degraded'), (18, 3, 'Minor'),
-- 19: search index corruption
(19, 18, 'RootCause'), (19, 8, 'Unavailable'),
-- 20: inventory drift
(20, 5, 'RootCause'),
-- 21: cache resize session churn
(21, 15, 'RootCause'), (21, 2, 'Degraded'), (21, 4, 'Minor'),
-- 22: stale partner shipment status
(22, 9, 'RootCause'), (22, 15, 'Minor');

SELECT 'Sample data loaded for ShopSphere.' AS status;
