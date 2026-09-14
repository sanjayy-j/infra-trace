-- =====================================================================
-- InfraTrace - Service Dependency & Impact Intelligence Platform
-- File   : database/schema/schema.sql
-- Purpose: Creates the InfraTrace database and all base tables.
-- DBMS   : MySQL 8.0+ (CHECK constraints require 8.0.16 or newer)
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

DROP DATABASE IF EXISTS infratrace;
CREATE DATABASE infratrace
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
USE infratrace;

-- ---------------------------------------------------------------------
-- 1. team
--    An engineering team that owns applications and components.
-- ---------------------------------------------------------------------
CREATE TABLE team (
    team_id      INT AUTO_INCREMENT PRIMARY KEY,
    team_name    VARCHAR(80)  NOT NULL,
    team_email   VARCHAR(120) NOT NULL,
    created_on   DATE         NOT NULL,

    CONSTRAINT uq_team_name  UNIQUE (team_name),
    CONSTRAINT uq_team_email UNIQUE (team_email)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 2. developer
--    An engineer. Belongs to at most one team.
--    team_id is nullable so a developer row survives team deletion
--    (ON DELETE SET NULL) and simply becomes unassigned.
-- ---------------------------------------------------------------------
CREATE TABLE developer (
    developer_id   INT AUTO_INCREMENT PRIMARY KEY,
    developer_name VARCHAR(80)  NOT NULL,
    email          VARCHAR(120) NOT NULL,
    job_role       VARCHAR(50)  NOT NULL,
    team_id        INT          NULL,
    joined_on      DATE         NOT NULL,

    CONSTRAINT uq_developer_email UNIQUE (email),
    CONSTRAINT fk_developer_team  FOREIGN KEY (team_id)
        REFERENCES team(team_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 3. application
--    A customer-facing or internal product built on top of components.
-- ---------------------------------------------------------------------
CREATE TABLE application (
    application_id   INT AUTO_INCREMENT PRIMARY KEY,
    application_name VARCHAR(100) NOT NULL,
    app_type         VARCHAR(20)  NOT NULL,
    owner_team_id    INT          NULL,
    description      VARCHAR(255) NULL,

    CONSTRAINT uq_application_name UNIQUE (application_name),
    CONSTRAINT chk_app_type CHECK (app_type IN ('Web','Mobile','Internal','API')),
    CONSTRAINT fk_application_team FOREIGN KEY (owner_team_id)
        REFERENCES team(team_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 4. component
--    The central entity: any deployable or managed piece of the system
--    (service, database, cache, queue, storage, gateway, external API).
--    owner_team_id is nullable on purpose - unowned components are a
--    real operational problem that InfraTrace is designed to surface.
-- ---------------------------------------------------------------------
CREATE TABLE component (
    component_id   INT AUTO_INCREMENT PRIMARY KEY,
    component_name VARCHAR(100) NOT NULL,
    component_type VARCHAR(20)  NOT NULL,
    owner_team_id  INT          NULL,
    criticality    VARCHAR(10)  NOT NULL DEFAULT 'Medium',
    tech_stack     VARCHAR(80)  NULL,
    is_active      TINYINT(1)   NOT NULL DEFAULT 1,
    created_on     DATE         NOT NULL,

    CONSTRAINT uq_component_name UNIQUE (component_name),
    CONSTRAINT chk_component_type CHECK (component_type IN
        ('Service','Database','Cache','Queue','Storage','Gateway','ExternalAPI')),
    CONSTRAINT chk_component_criticality CHECK (criticality IN
        ('Low','Medium','High','Critical')),
    CONSTRAINT chk_component_active CHECK (is_active IN (0,1)),
    CONSTRAINT fk_component_team FOREIGN KEY (owner_team_id)
        REFERENCES team(team_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 5. application_component
--    Many-to-many bridge: an application uses many components and a
--    component is reused by many applications.
-- ---------------------------------------------------------------------
CREATE TABLE application_component (
    application_id INT NOT NULL,
    component_id   INT NOT NULL,
    usage_notes    VARCHAR(150) NULL,

    CONSTRAINT pk_application_component PRIMARY KEY (application_id, component_id),
    CONSTRAINT fk_appcomp_application FOREIGN KEY (application_id)
        REFERENCES application(application_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    CONSTRAINT fk_appcomp_component FOREIGN KEY (component_id)
        REFERENCES component(component_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 6. dependency
--    The dependency graph. A SELF-REFERENCING many-to-many relationship
--    on component: component_id DEPENDS ON depends_on_id.
--
--    Reading direction for "Order Service -> Payment Service":
--      component_id  = Order Service    (the dependent / upstream caller)
--      depends_on_id = Payment Service  (the dependency / downstream)
--
--    Component details are never duplicated here - only ids are stored.
-- ---------------------------------------------------------------------
CREATE TABLE dependency (
    dependency_id   INT AUTO_INCREMENT PRIMARY KEY,
    component_id    INT NOT NULL,
    depends_on_id   INT NOT NULL,
    dependency_type VARCHAR(20)  NOT NULL DEFAULT 'Synchronous',
    is_critical     TINYINT(1)   NOT NULL DEFAULT 0,
    description     VARCHAR(150) NULL,

    -- the same edge may only be recorded once
    CONSTRAINT uq_dependency_edge UNIQUE (component_id, depends_on_id),

    -- NOTE: the "a component cannot depend on itself" rule would naturally be
    -- a CHECK (component_id <> depends_on_id) here. MySQL 8 refuses it:
    -- a column used in a CHECK constraint may not also carry a foreign-key
    -- referential action (error 3823), and both columns below use ON UPDATE
    -- CASCADE / ON DELETE CASCADE, which the dependency graph needs.
    -- The rule is therefore enforced in triggers.sql instead
    -- (trg_dependency_validate_insert / trg_dependency_validate_update).

    CONSTRAINT chk_dependency_type CHECK (dependency_type IN
        ('Synchronous','Asynchronous','Data','Config')),
    CONSTRAINT chk_dependency_critical CHECK (is_critical IN (0,1)),

    CONSTRAINT fk_dependency_component FOREIGN KEY (component_id)
        REFERENCES component(component_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    CONSTRAINT fk_dependency_depends_on FOREIGN KEY (depends_on_id)
        REFERENCES component(component_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 7. environment
--    Development / Staging / Production.
-- ---------------------------------------------------------------------
CREATE TABLE environment (
    environment_id   INT AUTO_INCREMENT PRIMARY KEY,
    environment_name VARCHAR(30) NOT NULL,
    region           VARCHAR(40) NOT NULL,
    is_production    TINYINT(1)  NOT NULL DEFAULT 0,

    CONSTRAINT uq_environment_name UNIQUE (environment_name),
    CONSTRAINT chk_environment_prod CHECK (is_production IN (0,1))
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 8. deployment
--    A release of one component version into one environment.
-- ---------------------------------------------------------------------
CREATE TABLE deployment (
    deployment_id  INT AUTO_INCREMENT PRIMARY KEY,
    component_id   INT         NOT NULL,
    environment_id INT         NOT NULL,
    version        VARCHAR(30) NOT NULL,
    deployed_by    INT         NULL,
    deployed_at    DATETIME    NOT NULL,
    status         VARCHAR(15) NOT NULL DEFAULT 'Success',
    is_rollback    TINYINT(1)  NOT NULL DEFAULT 0,

    -- status describes the OUTCOME of this deployment attempt only.
    -- is_rollback says whether this deployment put an EARLIER version back.
    -- Keeping the two ideas in separate columns is what makes the question
    -- "what is running right now?" answerable: it is the latest row with
    -- status = 'Success', whether or not that row was a rollback.
    -- A single 'RolledBack' status could not express this, because it
    -- conflated "was later rolled back" with "is itself a rollback".
    CONSTRAINT chk_deployment_status CHECK (status IN ('Success','Failed')),
    CONSTRAINT chk_deployment_rollback CHECK (is_rollback IN (0,1)),

    -- natural key: one component cannot be deployed to the same environment
    -- twice at the very same instant. Blocks accidental duplicate rows.
    CONSTRAINT uq_deployment_event UNIQUE (component_id, environment_id, deployed_at),

    CONSTRAINT fk_deployment_component FOREIGN KEY (component_id)
        REFERENCES component(component_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    CONSTRAINT fk_deployment_environment FOREIGN KEY (environment_id)
        REFERENCES environment(environment_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    CONSTRAINT fk_deployment_developer FOREIGN KEY (deployed_by)
        REFERENCES developer(developer_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 9. incident
--    An operational failure. Linked to components via incident_component.
--    SEV1 is the most severe level, SEV4 the least severe.
-- ---------------------------------------------------------------------
CREATE TABLE incident (
    incident_id    INT AUTO_INCREMENT PRIMARY KEY,
    title          VARCHAR(150) NOT NULL,
    severity       VARCHAR(10)  NOT NULL,
    status         VARCHAR(15)  NOT NULL DEFAULT 'Open',
    environment_id INT          NOT NULL,
    reported_by    INT          NULL,
    started_at     DATETIME     NOT NULL,
    resolved_at    DATETIME     NULL,
    root_cause     VARCHAR(255) NULL,

    -- Optional, and deliberately nullable: set ONLY when a post-incident
    -- review has CONFIRMED that a specific deployment caused this incident.
    -- This is different from the time-based correlation query, which merely
    -- reports "a deployment happened shortly before" - a suspicion, not a
    -- conclusion. Keeping the two apart stops the analysis from overclaiming.
    caused_by_deployment_id INT NULL,

    updated_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT chk_incident_severity CHECK (severity IN
        ('SEV1','SEV2','SEV3','SEV4')),
    CONSTRAINT chk_incident_status CHECK (status IN
        ('Open','Investigating','Mitigated','Resolved')),
    -- an incident cannot be resolved before it started
    CONSTRAINT chk_incident_time CHECK (resolved_at IS NULL OR resolved_at >= started_at),
    -- a resolved incident must carry a resolution timestamp
    CONSTRAINT chk_incident_resolved CHECK (status <> 'Resolved' OR resolved_at IS NOT NULL),

    CONSTRAINT fk_incident_environment FOREIGN KEY (environment_id)
        REFERENCES environment(environment_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    CONSTRAINT fk_incident_developer FOREIGN KEY (reported_by)
        REFERENCES developer(developer_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    -- SET NULL: if the deployment record is ever removed, the incident
    -- survives and simply loses its confirmed cause.
    CONSTRAINT fk_incident_caused_by FOREIGN KEY (caused_by_deployment_id)
        REFERENCES deployment(deployment_id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 10. incident_component
--     Many-to-many bridge: one incident affects several components and
--     one component is affected by several incidents over time.
-- ---------------------------------------------------------------------
CREATE TABLE incident_component (
    incident_id  INT NOT NULL,
    component_id INT NOT NULL,
    impact_level VARCHAR(15) NOT NULL,

    CONSTRAINT pk_incident_component PRIMARY KEY (incident_id, component_id),
    CONSTRAINT chk_impact_level CHECK (impact_level IN
        ('RootCause','Unavailable','Degraded','Minor')),
    CONSTRAINT fk_inccomp_incident FOREIGN KEY (incident_id)
        REFERENCES incident(incident_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    CONSTRAINT fk_inccomp_component FOREIGN KEY (component_id)
        REFERENCES component(component_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
) ENGINE=InnoDB;

-- =====================================================================
-- A NOTE ON CHECK CONSTRAINT METADATA
--
-- information_schema.check_constraints stores each CHECK clause with an
-- explicit character-set introducer on its string literals, and that
-- introducer is taken from the CLIENT's connection character set at the time
-- the DDL was parsed - not from the table or column.
--
-- Before this file declared its own charset, that meant the stored metadata
-- varied by platform: two constraints serialised as _latin1 when the schema
-- was built from a Linux container, and as _cp850 when built from a Windows
-- console, purely because the mysql client derives its default from the host.
--
-- The "SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci" at the top of this file
-- removes that variability. All eight enumerated CHECK constraints now
-- serialise identically as _utf8mb4 on every platform, matching the tables,
-- the columns and the database.
--
-- Verified on MySQL 8.0.46 on both Linux and Windows: 9 string-literal CHECKs
-- report utf8mb4 and 5 numeric CHECKs report no charset at all.
-- =====================================================================

-- =====================================================================
-- INDEXES
--
-- Note: InnoDB automatically creates an index on every foreign key
-- column, so single-column indexes on FK columns (for example
-- dependency.component_id) already exist and are not repeated here.
-- The indexes below are the ones that add value beyond that: composite
-- indexes that match the access patterns of the analytical queries.
-- =====================================================================

-- ownership lookups: "all components owned by team X, grouped by type"
CREATE INDEX idx_component_owner_type
    ON component (owner_team_id, component_type);

-- reverse dependency lookup ("who depends on me"), used by blast-radius
-- traversal; leading column is depends_on_id so the lookup is covered
CREATE INDEX idx_dependency_reverse
    ON dependency (depends_on_id, component_id);

-- "latest deployment of component C in environment E"
CREATE INDEX idx_deployment_comp_env_time
    ON deployment (component_id, environment_id, deployed_at DESC);

-- deployment timeline scans and "deployed just before an incident"
CREATE INDEX idx_deployment_time
    ON deployment (deployed_at);

-- severity dashboards and open-incident filters
CREATE INDEX idx_incident_severity_status
    ON incident (severity, status);

CREATE INDEX idx_incident_started_at
    ON incident (started_at);

SELECT 'Schema created: 10 tables with constraints and indexes.' AS status;
