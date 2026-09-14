-- =====================================================================
-- InfraTrace - 07. Blast-radius analysis (recursive)
-- File   : database/queries/07_blast_radius.sql
-- Requires: MySQL 8.0+ (WITH RECURSIVE)
--
-- =====================================================================
-- WHAT THIS IS, AND WHAT IT IS NOT
--
-- This is DEPENDENCY-BASED POTENTIAL IMPACT ANALYSIS. It answers exactly
-- one question: "following the dependency edges recorded in the dependency
-- table, what could be reached from a failure here?"
--
-- It does NOT model, because none of it is recorded in the schema:
--     * redundancy or replication   - a replicated database is treated as
--                                     a single point of failure
--     * failover                    - no notion of a standby taking over
--     * graceful degradation        - a component that would keep serving
--                                     a reduced feature set is reported as
--                                     fully affected
--     * circuit breakers, retries, timeouts, bulkheads
--     * asynchronous tolerance      - a queue consumer that can lag for an
--                                     hour is weighted the same as a
--                                     synchronous call that fails instantly
--     * live health or current traffic
--
-- So the correct reading of the output is "these components have a
-- dependency path to the failure", NOT "these components will go down".
-- It is an upper bound on the affected set, and a triage aid.
--
-- Treating asynchronous dependencies differently is the first planned
-- refinement - the data is already there in dependency.dependency_type.
-- =====================================================================
--
-- DIRECTION REMINDER
--   dependency.component_id  DEPENDS ON  dependency.depends_on_id
--   To find what BREAKS when X fails, walk UPWARDS: start at X and
--   repeatedly find rows whose depends_on_id is already in the set.
--   To find what X NEEDS, walk DOWNWARDS via depends_on_id.
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

-- ---------------------------------------------------------------------
-- R1. If Payment DB fails, what is affected, and who do we call?
--
--     Expected chain:
--       Payment DB <- Payment Service <- Order Service <- API Gateway
--
--     The anchor member seeds the traversal with Payment DB at depth 0.
--     The recursive member repeatedly adds every component that depends
--     on something already in the set. UNION (not UNION ALL) removes
--     components reachable by more than one path, which also guarantees
--     termination even if the data somehow contained a cycle.
-- ---------------------------------------------------------------------
WITH RECURSIVE impact (component_id, depth) AS (
    SELECT component_id, 0
    FROM component
    WHERE component_name = 'Payment DB'

    UNION

    SELECT d.component_id, im.depth + 1
    FROM dependency d
    JOIN impact im ON d.depends_on_id = im.component_id
    WHERE im.depth < 1000        -- safety net; UNION de-duplicates
)
SELECT
    MIN(im.depth)                       AS hops_from_failure,
    CASE MIN(im.depth)
        WHEN 0 THEN 'FAILED COMPONENT'
        WHEN 1 THEN 'directly affected'
        ELSE        'indirectly affected'
    END                                 AS impact_kind,
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED') AS team_to_notify,
    COALESCE(t.team_email, 'NOBODY')    AS contact
FROM impact im
JOIN component c ON im.component_id = c.component_id
LEFT JOIN team t ON c.owner_team_id = t.team_id
GROUP BY c.component_id, c.component_name, c.component_type,
         c.criticality, t.team_name, t.team_email
ORDER BY hops_from_failure, c.component_name;


-- ---------------------------------------------------------------------
-- R2. The same failure, showing the PROPAGATION PATH.
--     Builds a readable chain so the route can be explained, not just the
--     result set. UNION ALL here rather than UNION, because each distinct
--     path is itself the information being reported - two different routes
--     to the same component are two different things to understand.
-- ---------------------------------------------------------------------
WITH RECURSIVE impact_path (component_id, depth, path) AS (
    SELECT component_id, 0, CAST(component_name AS CHAR(1000))
    FROM component
    WHERE component_name = 'Payment DB'

    UNION ALL

    SELECT d.component_id, ip.depth + 1,
           CONCAT(ip.path, ' -> ', c.component_name)
    FROM dependency d
    JOIN impact_path ip ON d.depends_on_id = ip.component_id
    JOIN component   c  ON d.component_id  = c.component_id
    WHERE ip.depth < 1000
)
SELECT depth AS hops, path AS propagation_path
FROM impact_path
WHERE depth > 0
ORDER BY depth, path;


-- ---------------------------------------------------------------------
-- R3. Which APPLICATIONS does a Payment DB failure reach?
--     Extends the traversal past components to the customer-facing
--     applications. This is the answer to "what does the customer see?",
--     which is the version of the question a manager actually asks.
-- ---------------------------------------------------------------------
WITH RECURSIVE impact (component_id) AS (
    SELECT component_id FROM component WHERE component_name = 'Payment DB'
    UNION
    SELECT d.component_id
    FROM dependency d JOIN impact im ON d.depends_on_id = im.component_id
)
SELECT
    a.application_name,
    a.app_type,
    COALESCE(t.team_name, 'UNASSIGNED') AS application_owner,
    COUNT(DISTINCT c.component_id)      AS affected_components_used,
    GROUP_CONCAT(DISTINCT c.component_name ORDER BY c.component_name SEPARATOR ', ')
                                        AS affected_components
FROM impact im
JOIN component c              ON im.component_id   = c.component_id
JOIN application_component ac ON c.component_id    = ac.component_id
JOIN application a            ON ac.application_id = a.application_id
LEFT JOIN team t              ON a.owner_team_id   = t.team_id
GROUP BY a.application_id, a.application_name, a.app_type, t.team_name
ORDER BY affected_components_used DESC, a.application_name;


-- ---------------------------------------------------------------------
-- R4. Which TEAMS would need to be paged by a Payment DB failure?
--     The escalation list, de-duplicated to one row per team, with the
--     nearest affected component so each team knows why they are on it.
-- ---------------------------------------------------------------------
WITH RECURSIVE impact (component_id, depth) AS (
    SELECT component_id, 0 FROM component WHERE component_name = 'Payment DB'
    UNION
    SELECT d.component_id, im.depth + 1
    FROM dependency d JOIN impact im ON d.depends_on_id = im.component_id
    WHERE im.depth < 1000
)
SELECT
    COALESCE(t.team_name, '*** UNOWNED - NOBODY TO PAGE ***') AS team_to_page,
    COALESCE(t.team_email, 'none')                            AS contact,
    MIN(im.depth)                                             AS nearest_hop,
    COUNT(DISTINCT c.component_id)                            AS components_affected,
    GROUP_CONCAT(DISTINCT c.component_name ORDER BY c.component_name SEPARATOR ', ')
                                                              AS their_components
FROM impact im
JOIN component c ON im.component_id = c.component_id
LEFT JOIN team t ON c.owner_team_id = t.team_id
GROUP BY c.owner_team_id, t.team_name, t.team_email
ORDER BY nearest_hop, components_affected DESC;


-- ---------------------------------------------------------------------
-- R5. Blast radius for EVERY component, ranked.
--     Runs the traversal from all 19 origins at once: the anchor seeds one
--     row per component as its own origin, and the recursion propagates
--     that origin outwards. This is the platform-wide risk ranking, and it
--     is the query behind "which components have the largest potential
--     blast radius?".
--
--     max_propagation_depth is the length of the longest chain from that
--     component - a component with a wide but shallow blast radius fails
--     differently from one with a narrow but deep chain.
-- ---------------------------------------------------------------------
WITH RECURSIVE spread (origin_id, component_id, depth) AS (
    SELECT component_id, component_id, 0 FROM component

    UNION

    SELECT s.origin_id, d.component_id, s.depth + 1
    FROM dependency d
    JOIN spread s ON d.depends_on_id = s.component_id
    WHERE s.depth < 1000
)
SELECT
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED')  AS owning_team,
    COUNT(DISTINCT s.component_id) - 1   AS components_affected,
    MAX(s.depth)                         AS max_propagation_depth,
    COUNT(DISTINCT ac.application_id)    AS applications_affected
FROM spread s
JOIN component c              ON s.origin_id    = c.component_id
LEFT JOIN team t              ON c.owner_team_id = t.team_id
LEFT JOIN application_component ac ON s.component_id = ac.component_id
GROUP BY c.component_id, c.component_name, c.component_type,
         c.criticality, t.team_name
ORDER BY components_affected DESC, max_propagation_depth DESC, c.component_name;


-- ---------------------------------------------------------------------
-- R6. The opposite direction: everything the API Gateway ultimately needs.
--     Walks DOWNWARDS through depends_on_id. This is the full dependency
--     footprint of the customer-facing entry point - in other words, the
--     complete list of things that can break the website.
-- ---------------------------------------------------------------------
WITH RECURSIVE needs (component_id, depth) AS (
    SELECT component_id, 0 FROM component WHERE component_name = 'API Gateway'

    UNION

    SELECT d.depends_on_id, n.depth + 1
    FROM dependency d JOIN needs n ON d.component_id = n.component_id
    WHERE n.depth < 1000
)
SELECT
    MIN(n.depth)                        AS depth,
    c.component_name                    AS required_component,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED') AS owning_team,
    CASE WHEN NOT EXISTS (SELECT 1 FROM dependency d
                          WHERE d.component_id = c.component_id)
         THEN 'foundation - depends on nothing' ELSE '' END AS note
FROM needs n
JOIN component c ON n.component_id = c.component_id
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE n.depth > 0
GROUP BY c.component_id, c.component_name, c.component_type,
         c.criticality, t.team_name
ORDER BY depth, required_component;


-- ---------------------------------------------------------------------
-- R7. Critical-path-only blast radius, contrasted with the full one.
--     A first step towards the refinement named at the top of this file:
--     restrict the traversal to edges flagged is_critical = 1 and it
--     answers a sharper question - "what fails HARD?" rather than "what
--     has any dependency path at all?".
--
--     The difference between the two columns is the set of components that
--     are connected but not critically so. That gap is exactly what the
--     current model over-reports.
-- ---------------------------------------------------------------------
WITH RECURSIVE
critical_impact (origin_id, component_id) AS (
    SELECT component_id, component_id FROM component
    UNION
    SELECT ci.origin_id, d.component_id
    FROM dependency d
    JOIN critical_impact ci ON d.depends_on_id = ci.component_id
    WHERE d.is_critical = 1                 -- only hard dependencies
),
all_impact (origin_id, component_id) AS (
    SELECT component_id, component_id FROM component
    UNION
    SELECT ai.origin_id, d.component_id
    FROM dependency d
    JOIN all_impact ai ON d.depends_on_id = ai.component_id
)
SELECT
    c.component_name,
    c.criticality,
    (SELECT COUNT(DISTINCT ai.component_id) - 1 FROM all_impact ai
      WHERE ai.origin_id = c.component_id)      AS full_blast_radius,
    (SELECT COUNT(DISTINCT ci.component_id) - 1 FROM critical_impact ci
      WHERE ci.origin_id = c.component_id)      AS critical_path_blast_radius,
    (SELECT COUNT(DISTINCT ai.component_id) FROM all_impact ai
      WHERE ai.origin_id = c.component_id)
    - (SELECT COUNT(DISTINCT ci.component_id) FROM critical_impact ci
        WHERE ci.origin_id = c.component_id)    AS over_reported_by
FROM component c
WHERE c.is_active = 1
ORDER BY full_blast_radius DESC, c.component_name;
