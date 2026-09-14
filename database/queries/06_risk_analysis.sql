-- =====================================================================
-- InfraTrace - 06. Risk analysis
-- File   : database/queries/06_risk_analysis.sql
--
-- Combining structure (blast radius), importance (criticality), ownership
-- and history (incidents) into a ranking of what to worry about.
--
-- IMPORTANT, and stated up front: these are dependency-based POTENTIAL
-- risk heuristics. They rank components against one another. They do NOT
-- model redundancy, failover, replication, circuit breakers, graceful
-- degradation or live health - none of that is recorded in the schema, so
-- none of it can be computed. See the header of 07_blast_radius.sql.
--
-- SQL demonstrated: stored functions, window functions (RANK, NTILE,
-- PERCENT_RANK, AVG OVER PARTITION), CASE, HAVING, correlated subqueries.
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
-- K1. Blast-radius risk score for every active component.
--     fn_risk_score() = blast radius x criticality weight, so the formula
--     lives in exactly one place and cannot drift between the SQL, the
--     stored procedures and the API.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED')       AS owning_team,
    fn_direct_dependent_count(c.component_id) AS direct_dependents,
    fn_blast_radius_count(c.component_id)     AS total_affected,
    fn_dependency_depth(c.component_id)       AS dependency_depth,
    fn_risk_score(c.component_id)             AS risk_score,
    CASE
        WHEN fn_blast_radius_count(c.component_id) >= 5 THEN 'SEVERE'
        WHEN fn_blast_radius_count(c.component_id) >= 3 THEN 'HIGH'
        WHEN fn_blast_radius_count(c.component_id) >= 1 THEN 'MODERATE'
        ELSE 'ISOLATED'
    END                                       AS blast_radius_band
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
ORDER BY risk_score DESC, total_affected DESC, c.component_name;


-- ---------------------------------------------------------------------
-- K2. Risk ranking with window functions.
--     RANK()         - position overall, ties share a rank
--     DENSE_RANK()   - same, but without gaps after a tie
--     NTILE(4)       - split into quartiles; quartile 1 is the worst
--     PERCENT_RANK() - relative standing as a 0..1 fraction
--     AVG() OVER (PARTITION BY type) - each component's type average,
--                      shown ON THE SAME ROW, so a component can be
--                      compared against its own kind rather than against
--                      a database mixed in with caches and queues.
-- ---------------------------------------------------------------------
SELECT
    component_name,
    component_type,
    criticality,
    owning_team,
    blast_radius,
    risk_score,
    RANK()       OVER (ORDER BY risk_score DESC)             AS risk_rank,
    DENSE_RANK() OVER (ORDER BY risk_score DESC)             AS risk_dense_rank,
    NTILE(4)     OVER (ORDER BY risk_score DESC)             AS risk_quartile,
    ROUND(PERCENT_RANK() OVER (ORDER BY risk_score), 2)      AS percentile,
    ROUND(AVG(blast_radius) OVER (PARTITION BY component_type), 1)
                                                             AS avg_blast_for_type,
    CASE WHEN blast_radius > AVG(blast_radius) OVER (PARTITION BY component_type)
         THEN 'above average for its type' ELSE '' END        AS comparison
FROM (
    SELECT
        c.component_id,
        c.component_name,
        c.component_type,
        c.criticality,
        COALESCE(t.team_name, 'UNASSIGNED')   AS owning_team,
        fn_blast_radius_count(c.component_id) AS blast_radius,
        fn_risk_score(c.component_id)         AS risk_score
    FROM component c
    LEFT JOIN team t ON c.owner_team_id = t.team_id
    WHERE c.is_active = 1
) scored
ORDER BY risk_score DESC, component_name;


-- ---------------------------------------------------------------------
-- K3. Single points of failure.
--     A component qualifies when several risk factors coincide. Each
--     factor is cheap on its own; it is the COMBINATION that matters, so
--     the query builds an explicit reason string rather than just a score.
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    c.component_type,
    c.criticality,
    COALESCE(t.team_name, 'UNASSIGNED')       AS owning_team,
    fn_blast_radius_count(c.component_id)     AS blast_radius,
    fn_incident_count(c.component_id)         AS past_incidents,
    -- one point per risk factor present
    (CASE WHEN fn_blast_radius_count(c.component_id) >= 3   THEN 1 ELSE 0 END) +
    (CASE WHEN c.criticality IN ('Critical','High')          THEN 1 ELSE 0 END) +
    (CASE WHEN c.owner_team_id IS NULL                       THEN 1 ELSE 0 END) +
    (CASE WHEN fn_incident_count(c.component_id) >= 2        THEN 1 ELSE 0 END) +
    (CASE WHEN NOT EXISTS (SELECT 1 FROM dependency d
                           WHERE d.component_id = c.component_id) THEN 1 ELSE 0 END)
                                              AS risk_factors,
    CONCAT_WS(' | ',
        CASE WHEN fn_blast_radius_count(c.component_id) >= 3
             THEN 'wide blast radius' END,
        CASE WHEN c.criticality IN ('Critical','High')
             THEN CONCAT(c.criticality, ' criticality') END,
        CASE WHEN c.owner_team_id IS NULL
             THEN 'NO OWNER' END,
        CASE WHEN fn_incident_count(c.component_id) >= 2
             THEN 'repeat incidents' END,
        CASE WHEN NOT EXISTS (SELECT 1 FROM dependency d
                              WHERE d.component_id = c.component_id)
             THEN 'foundation component (depends on nothing)' END
    )                                         AS why
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
HAVING risk_factors >= 2
ORDER BY risk_factors DESC, blast_radius DESC, c.component_name;


-- ---------------------------------------------------------------------
-- K4. Change risk: what is most dangerous to touch right now?
--     Combines three independent signals:
--       structural  - how far a failure would spread
--       historical  - has this component caused incidents before
--       temporal    - has it changed recently (recent change plus wide
--                     blast radius is the classic pre-incident shape)
-- ---------------------------------------------------------------------
SELECT
    c.component_name,
    COALESCE(t.team_name, 'UNASSIGNED')   AS owning_team,
    fn_blast_radius_count(c.component_id) AS blast_radius,
    fn_incident_count(c.component_id)     AS past_incidents,
    (SELECT MAX(d.deployed_at)
       FROM deployment d
       JOIN environment e ON d.environment_id = e.environment_id
      WHERE d.component_id = c.component_id
        AND e.is_production = 1
        AND d.status = 'Success')          AS last_production_release,
    (SELECT DATEDIFF((SELECT MAX(deployed_at) FROM deployment), MAX(d.deployed_at))
       FROM deployment d
       JOIN environment e ON d.environment_id = e.environment_id
      WHERE d.component_id = c.component_id
        AND e.is_production = 1
        AND d.status = 'Success')          AS days_since_release,
    CASE
        WHEN fn_blast_radius_count(c.component_id) >= 5
             AND fn_incident_count(c.component_id) >= 2
            THEN 'HIGHEST - wide reach and a bad history'
        WHEN fn_blast_radius_count(c.component_id) >= 5
            THEN 'HIGH - a failure here spreads widely'
        WHEN fn_incident_count(c.component_id) >= 3
            THEN 'HIGH - repeatedly involved in incidents'
        WHEN fn_blast_radius_count(c.component_id) >= 3
            THEN 'MEDIUM'
        ELSE 'LOW'
    END                                    AS change_risk
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
ORDER BY
    FIELD(change_risk, 'HIGHEST - wide reach and a bad history',
                       'HIGH - a failure here spreads widely',
                       'HIGH - repeatedly involved in incidents',
                       'MEDIUM', 'LOW'),
    blast_radius DESC,
    c.component_name;


-- ---------------------------------------------------------------------
-- K5. Team risk exposure.
--     Aggregates component-level risk up to the owning team, so it is
--     clear which team carries the most of the platform's total risk.
--     The UNASSIGNED row is the one to read first.
-- ---------------------------------------------------------------------
SELECT
    COALESCE(t.team_name, 'UNASSIGNED')                          AS team,
    COUNT(c.component_id)                                        AS components,
    SUM(fn_risk_score(c.component_id))                           AS total_risk_score,
    ROUND(AVG(fn_risk_score(c.component_id)), 1)                 AS avg_risk_score,
    MAX(fn_risk_score(c.component_id))                           AS worst_component_score,
    SUM(fn_incident_count(c.component_id))                       AS total_incident_involvements,
    ROUND(100.0 * SUM(fn_risk_score(c.component_id))
          / (SELECT SUM(fn_risk_score(component_id))
               FROM component WHERE is_active = 1), 1)           AS pct_of_platform_risk,
    (SELECT c2.component_name
       FROM component c2
      WHERE c2.owner_team_id <=> c.owner_team_id AND c2.is_active = 1
      ORDER BY fn_risk_score(c2.component_id) DESC, c2.component_name
      LIMIT 1)                                                   AS riskiest_component
FROM component c
LEFT JOIN team t ON c.owner_team_id = t.team_id
WHERE c.is_active = 1
GROUP BY c.owner_team_id, t.team_name
ORDER BY total_risk_score DESC;
