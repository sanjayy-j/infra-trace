# InfraTrace — Requirements Specification

**Project:** InfraTrace — Service Dependency & Impact Intelligence Platform
**Course:** Database Management Systems
**DBMS:** MySQL 8.0.46 (InnoDB)

---

## 1. Problem Statement

A modern software product is not a single program. It is a network of
applications, services, databases, caches, message queues and third-party APIs
that call one another. ShopSphere, the fictional e-commerce company modelled
here, runs 19 such components across 4 applications and 3 environments.

When any one of those components fails, is upgraded, or has to be taken offline,
three questions must be answered immediately:

1. **What does this component depend on?** — where is the fault likely to be?
2. **What depends on this component?** — who is affected, and how far does it spread?
3. **Who owns it?** — which team must be contacted?

In most organisations this knowledge is scattered across spreadsheets, wiki
pages, architecture diagrams that have gone stale, and the memory of whichever
engineer has been there longest. None of that can be queried.

## 2. The Existing Problem

- Dependency knowledge is **undocumented or stale**; diagrams drift from reality.
- Ownership is **implicit**. Components quietly become unowned during a
  reorganisation, and nobody notices until an incident.
- Impact analysis is **manual**. During an outage, engineers reconstruct the
  dependency chain by hand, under time pressure.
- Deployment history and incident history live in **separate tools**, so the
  obvious question "what changed just before this broke?" is hard to answer.
- Because the information is unstructured, it **cannot be aggregated**.
  Questions like "which component would hurt us most if it failed?" have no
  answer at all.

## 3. Proposed Solution

InfraTrace models the infrastructure estate as a **normalised relational
database** and makes impact analysis a matter of running a query.

The core design decision is that dependencies are stored as a
**self-referencing many-to-many relationship** on `component`, through a
`dependency` bridge table. A dependency is a row — an edge in a graph — not a
duplicated copy of component information. This means:

- the graph can be traversed with **recursive SQL** (`WITH RECURSIVE`) to any
  depth, producing blast-radius analysis;
- ownership, deployment and incident data all attach to the same `component`
  key, so a single join connects "what broke", "what it affects" and "who to
  call";
- the graph can be **validated by the database itself** — triggers reject any
  write that would create a cycle.

## 4. Objectives

| # | Objective | Status |
|---|---|---|
| O1 | Normalised relational schema (BCNF) for the whole estate | Met — 10 tables |
| O2 | Dependency graph as a self-referencing relationship with referential integrity | Met |
| O3 | Realistic dataset that produces meaningful analytical results | Met — 220 rows, deliberate edge cases |
| O4 | SQL that answers real operational questions | Met — 41 queries in 8 categories |
| O5 | Multi-level blast-radius analysis via recursive CTEs | Met — 7 recursive queries |
| O6 | Demonstrate core DBMS features | Met — 4 views, 6 functions, 5 procedures, 5 triggers, 6 indexes |
| O7 | Demonstrate transactions, isolation and concurrency control | Met — 6 runnable demos, verified with concurrent sessions |
| O8 | Demonstrate query optimisation with measured evidence | Met — `EXPLAIN ANALYZE` before/after at 2,000-component scale |
| O9 | Prevent circular dependencies at the database level | Met — prevention **and** detection |
| O10 | Expose the analysis through a database-backed API and dashboard | Met — 27 endpoints, 6-view dashboard |

## 5. Functional Requirements

### Core data model

| ID | Requirement | Where |
|---|---|---|
| FR1 | Store engineering teams and the developers belonging to them | `team`, `developer` |
| FR2 | Store applications and the components each uses (M:N) | `application_component` |
| FR3 | Store components classified by type and criticality | `component` |
| FR4 | Record that one component depends on another, with type and critical-path flag | `dependency` |
| FR5 | Prevent a component depending on itself, and prevent duplicate edges | trigger + `uq_dependency_edge` |
| FR6 | Record deployments of a version into an environment, with status and engineer | `deployment` |
| FR7 | Record incidents with severity, status and timeline, linked to every affected component | `incident`, `incident_component` |
| FR8 | Distinguish a **confirmed** deployment cause from a time correlation | `incident.caused_by_deployment_id` |
| FR9 | Record that a deployment was a rollback, separately from whether it succeeded | `deployment.is_rollback` |

### Analysis

| ID | Requirement | Where |
|---|---|---|
| FR10 | Report all direct dependencies of a component | `03_dependency_analysis.sql`, `sp_get_dependencies` |
| FR11 | Report all direct dependents of a component | same |
| FR12 | Compute the transitive blast radius using recursive traversal | `07_blast_radius.sql`, `fn_blast_radius_count` |
| FR13 | Report the propagation **path**, not only the affected set | `07_blast_radius.sql` R2 |
| FR14 | Report the applications and teams a failure would reach | R3, R4 |
| FR15 | Rank components by potential blast radius and criticality | `fn_risk_score`, `06_risk_analysis.sql` |
| FR16 | Identify components with no owning team | `02_ownership_analysis.sql` |
| FR17 | Report what is running in production, at which version | `production_infrastructure_view` |
| FR18 | Correlate production deployments with subsequent incidents | `08_advanced_sql.sql` A1 |
| FR19 | Aggregate incidents by severity, component and responsible team | `04_incident_analysis.sql` |
| FR20 | Report incident trend over time | `04_incident_analysis.sql` I5 (window functions) |
| FR21 | Detect any circular dependency in stored data | `sp_detect_dependency_cycles` |

### Integrity and concurrency

| ID | Requirement | Where |
|---|---|---|
| FR22 | Reject a dependency edge that would close a cycle of any length up to the configured recursion limit (default 1000) | `fn_would_create_cycle` + triggers |
| FR23 | Reject a production deployment of a retired component, on INSERT and UPDATE | deployment triggers |
| FR24 | Maintain incident resolution timestamps automatically | `trg_incident_set_resolved_at` |
| FR25 | Reject a duplicate deployment event | `uq_deployment_event` |

### Interface

| ID | Requirement | Where |
|---|---|---|
| FR26 | Expose the analysis over HTTP without moving logic out of SQL | `backend/` — 27 read-only endpoints |
| FR27 | Provide a dashboard demonstrating the database's capabilities | `frontend/index.html` |
| FR28 | Visualise the dependency graph and a component's impact subgraph | dashboard, Graph and Explorer views |

## 6. Non-Functional Requirements

| ID | Requirement | How it is met |
|---|---|---|
| NFR1 | **Integrity** — every relationship enforced by a foreign key; invalid enumerated values rejected | 15 FKs, 14 CHECKs, 8 UNIQUEs, 5 triggers |
| NFR2 | **Normalisation** — BCNF, with functional dependencies documented | [`normalization.md`](../database/normalization.md) |
| NFR3 | **Reproducibility** — the database is created entirely from version-controlled SQL, with no manual steps | `database/setup.sql` |
| NFR4 | **Performance** — traversal and filtering supported by indexes chosen from measured access patterns | [`query-optimization.md`](../database/query-optimization.md) |
| NFR5 | **Verifiability** — a failing setup must be obvious | 166 DB tests + 45 API tests |
| NFR6 | **Readability** — self-explanatory names; every query states the business question it answers | all `database/queries/` files |
| NFR7 | **Independence** — the database remains fully usable through any SQL client with no application layer | API and dashboard are strictly optional |
| NFR8 | **Security** — no credentials in source; parameterised SQL; least-privilege database account | `.env.example`, `queries.py`, CORS allowlist |
| NFR9 | **Honesty** — the system must not claim analysis it does not perform | blast-radius caveat returned in the API payload and shown on the dashboard |

## 7. Scope

### In scope (implemented and verified)

- 10-table BCNF schema with 15 FKs, 14 CHECKs, 8 UNIQUEs, 6 indexes
- ShopSphere dataset — 220 rows with deliberate edge cases
- 41 analytical queries across 8 category files
- 7 recursive blast-radius queries
- 4 views, 6 stored functions, 5 stored procedures, 5 triggers
- Full circular-dependency **prevention** plus a detection audit
- 6 transaction and concurrency demonstrations, verified with concurrent sessions
- Query-optimisation study with measured `EXPLAIN ANALYZE` before/after
- 27-endpoint read-only API; 6-view dashboard with graph visualisation
- 211 automated tests
- Documentation including a DBMS concept mapping

### Out of scope (and not claimed)

- Write endpoints; authentication and authorisation
- Automatic discovery of dependencies from live systems
- Integration with cloud providers, Kubernetes, CI/CD or monitoring tools
- Machine learning or natural-language querying
- Modelling of redundancy, failover, graceful degradation or circuit breakers
- High availability, replication, sharding, production deployment

## 8. Known Limitations

Recorded so the analysis is not over-read:

1. **Blast radius is an upper bound.** It reports components with a dependency
   path to the failure. It does not model redundancy, failover, graceful
   degradation, circuit breakers, retries or live health — none of which are in
   the schema. Asynchronous dependencies are currently weighted the same as
   synchronous ones, though `dependency_type` already records the distinction.
2. **The risk score is a heuristic**, not a probability. It ranks components
   against each other; the absolute number has no unit.
3. **Cycle prevention on `UPDATE` is conservative.** The check runs before the
   update applies, so the row's old values are still present during the
   reachability walk. Re-pointing an edge in place can be refused even when the
   resulting graph would be acyclic. Refusing wrongly is the safe direction, and
   the normal workflow is DELETE then INSERT.
4. **Recursion is capped at 20 levels** as a safety stop.
5. **Correlation is not causation.** The 7-day deploy-to-incident window
   produces a suspicion; only `caused_by_deployment_id` records a conclusion.
   The two are deliberately never merged.
6. **Performance measurements come from a generated dataset.** At 19 components
   MySQL correctly ignores every index, so the optimisation study runs against a
   separate 2,000-component schema. Stated wherever a number is quoted.

## 9. Future Scope

| Phase | Extension |
|---|---|
| Next | Weight blast radius by `dependency_type` — an async consumer tolerates an outage far longer than a synchronous caller. The data is already recorded. |
| Next | Model redundancy and failover, so a replicated database is no longer treated as a single point of failure. |
| Next | Write endpoints with authentication, so the graph can be maintained through the API. |
| Later | Historical versioning of the dependency graph — "what did the architecture look like *during* that incident?" |
| Later | CI pipeline running both test suites against a MySQL service container. |
| Later | Optional integrations importing dependencies from service meshes or CI/CD metadata. |
| Optional | AI as a layer **on top of** the database: incident correlation, anomaly detection, natural-language querying. Explicitly not part of the current system. |

Every item above adds to the database. The database defined here remains the
core of the system and stays useful without any of them.
