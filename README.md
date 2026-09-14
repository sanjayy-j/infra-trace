# InfraTrace
### Service Dependency & Impact Intelligence Platform

A database-centric platform for modelling how software services, applications
and infrastructure depend on one another — and for answering, in SQL, the
questions that matter when something breaks.

**The relational database is the product.** The API and dashboard are thin
layers over it; remove them and every capability below still works from a MySQL
prompt.

```
        real software infrastructure
                    |
      normalised relational database        <-- the project
                    |
    dependency graph as a relation (self-referencing M:N)
                    |
         SQL analysis, incl. recursive CTEs
                    |
   impact  .  ownership  .  incident intelligence
                    |
          optional REST API + dashboard      <-- thin layers
```

Everything above the dashed line is SQL. For example, this chain:

```
Payment DB  <-  Payment Service  <-  Order Service  <-  API Gateway
   hop 0           hop 1               hop 2             hop 3
```

is produced by a recursive SQL query, not by application code.

---

## Contents

1. [Overview](#1-overview) · 2. [Problem statement](#2-problem-statement) ·
3. [Motivation](#3-motivation) · 4. [Objectives](#4-objectives) ·
5. [Key features](#5-key-features) · 6. [System architecture](#6-system-architecture) ·
7. [Database architecture](#7-database-architecture) · 8. [ER diagram](#8-er-diagram) ·
9. [Schema overview](#9-schema-overview) · 10. [Normalisation](#10-normalisation) ·
11. [SQL capabilities](#11-sql-capabilities) · 12. [Blast-radius analysis](#12-blast-radius-analysis) ·
13. [Transactions & concurrency](#13-transactions--concurrency) ·
14. [Query optimisation](#14-query-optimisation) · 15. [Backend](#15-backend) ·
16. [Frontend](#16-frontend) · 17. [Setup](#17-setup) · 18. [Testing](#18-testing) ·
19. [Example queries](#19-example-queries) · 20. [Demo walkthrough](#20-demo-walkthrough) ·
21. [Limitations](#21-limitations) · 22. [Future scope](#22-future-scope) ·
23. [Project structure](#23-project-structure) · 24. [Team contribution](#24-team-contribution) ·
25. [License](#25-license)

---

## 1. Overview

InfraTrace stores an infrastructure estate as a normalised relational database
and turns impact analysis into a query. Its distinguishing idea is that a
**dependency graph is modelled relationally** — as a self-referencing
many-to-many relationship — and then traversed with recursive SQL.

Everything below has been executed against **MySQL 8.0.46**, verified on both
a native Windows install and a Linux container. Where a number is quoted, it
is measured.

| | |
|---|---|
| Tables | 10 (BCNF) |
| Constraints | 15 foreign keys · 14 CHECK · 8 UNIQUE |
| Sample data | 220 rows with deliberate edge cases |
| Analytical queries | 41, in 8 category files |
| Recursive queries | 7 |
| Views · Functions · Procedures · Triggers · Indexes | 4 · 6 · 5 · 5 · 6 |
| Transaction demos | 6, verified with concurrent sessions |
| API | 27 read-only endpoints (FastAPI) |
| Dashboard | 6 views incl. graph visualisation |
| Tests | **166 database + 45 API = 211**, all passing |

## 2. Problem Statement

When a component fails, is upgraded, or must be taken offline, three questions
need answering immediately:

1. **What does this component depend on?** — where is the fault likely to be?
2. **What depends on it?** — who is affected, and how far does it spread?
3. **Who owns it?** — which team must be contacted?

In most organisations that knowledge lives in stale diagrams, scattered wiki
pages and the memory of individual engineers. None of it can be queried, so
impact analysis is done by hand, under time pressure, during an outage.

## 3. Motivation

The interesting technical claim is that a **graph problem** — usually presented
as the thing relational databases are bad at — fits the relational model
cleanly. One bridge table holds the entire dependency graph; `WITH RECURSIVE`
computes its transitive closure; triggers guarantee it stays acyclic. No graph
database is needed, and the same rows that answer "what breaks?" also join
directly to ownership, deployment and incident history.

## 4. Objectives

| # | Objective | Status |
|---|---|---|
| O1 | Normalised relational schema (BCNF) | 10 tables |
| O2 | Dependency graph as a self-referencing relationship | `dependency` bridge table |
| O3 | Realistic dataset producing meaningful results | 220 rows, edge cases included |
| O4 | SQL answering real operational questions | 41 queries |
| O5 | Multi-level blast radius via recursive CTEs | 7 recursive queries |
| O6 | Demonstrate core DBMS features | views, functions, procedures, triggers, indexes |
| O7 | Demonstrate transactions and concurrency control | 6 demos, measured |
| O8 | Demonstrate query optimisation with evidence | `EXPLAIN ANALYZE`, before/after |
| O9 | Prevent circular dependencies in the database | prevention **and** detection |
| O10 | Expose the analysis over an API and dashboard | 27 endpoints, 6 views |

## 5. Key Features

| Feature | How it works |
|---|---|
| **Dependency graph** | `dependency` — a self-referencing M:N bridge on `component`. One row = one edge. |
| **Blast-radius analysis** | `WITH RECURSIVE` traversal upwards to any depth, with the propagation path. |
| **Cycle prevention** | `fn_would_create_cycle()` is a reachability test; triggers reject any edge that would close a loop — of any length up to the recursion limit (default 1000), not just self- or mutual-dependency — on INSERT *and* UPDATE. |
| **Cycle detection audit** | `sp_detect_dependency_cycles()` finds cycles in stored data, for when triggers were bypassed by a bulk load. |
| **Ownership tracking** | Unowned components are **surfaced**, not hidden — `owner_team_id` is deliberately nullable. |
| **Deployment history** | Every version into every environment, with `status` and `is_rollback` as separate facts. |
| **Incident impact** | M:N link classified by `impact_level` (`RootCause`, `Unavailable`, `Degraded`, `Minor`). |
| **Correlation vs causation** | A 7-day time window gives a suspicion; `caused_by_deployment_id` records a confirmed conclusion. Never merged. |
| **Risk scoring** | `fn_risk_score()` = blast radius × criticality weight, defined **once** so SQL, procedures and dashboard cannot disagree. |

## 6. System Architecture

```
┌──────────────────────────────────────────────────────────┐
│  frontend/   static dashboard, 6 views, SVG graph         │
│              no framework, no build step                  │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP (GET only, CORS allowlist)
┌────────────────────────▼─────────────────────────────────┐
│  backend/    FastAPI · 27 read-only endpoints             │
│              binds parameters, runs SQL, shapes JSON      │
│              NO analysis logic lives here                 │
└────────────────────────┬─────────────────────────────────┘
                         │ PyMySQL · SELECT + EXECUTE only
┌────────────────────────▼─────────────────────────────────┐
│  MySQL 8  ── THE SOURCE OF TRUTH                          │
│    10 tables · 4 views · 6 functions · 5 procedures       │
│    5 triggers · 6 indexes · recursive CTEs                │
│    blast radius, risk score, cycle detection computed HERE│
└──────────────────────────────────────────────────────────┘
```

The layering rule is strict and checkable: every SQL statement the API runs is
in one file, [`backend/app/queries.py`](backend/app/queries.py). If blast radius
were reimplemented in Python there would be two definitions free to disagree.

## 7. Database Architecture

```
                      team
                       │
        ┌──────────────┼───────────────┐
        │              │               │
    developer     application      component ──────┐
        │              │               │  ▲        │
        │              └─── application_component   │
        │                                  │        │
        │                            dependency ────┘
        │                        (self-referencing M:N:
        │                         component_id → depends_on_id)
        │
        ├──► deployment ◄── environment
        │         ▲              │
        │         └── incident ◄─┘
        │              │   (caused_by_deployment_id)
        └──────────────┤
                incident_component ──► component
```

**Reading direction.** `dependency.component_id` **depends on**
`dependency.depends_on_id`.

- *"What does X need?"* → filter `component_id = X`, follow `depends_on_id`.
- *"What breaks if X fails?"* → filter `depends_on_id = X`, follow
  `component_id`. Repeat recursively = blast radius.

## 8. ER Diagram

Mermaid source (version-controlled, renders on GitHub):
**[`docs/diagrams/er-diagram.md`](docs/diagrams/er-diagram.md)**

It contains the full ER diagram, a close-up of the self-referencing
relationship, the ShopSphere dependency graph, and the Payment DB blast-radius
chain.

## 9. Schema Overview

| Table | Purpose | Key |
|---|---|---|
| `team` | engineering teams | `team_id` |
| `developer` | engineers; at most one team | `developer_id` |
| `application` | customer-facing / internal products | `application_id` |
| `component` | **central entity** — service, database, cache, queue, storage, gateway, external API | `component_id` |
| `application_component` | bridge: which components an app uses | composite |
| `dependency` | **the graph** — self-referencing bridge | `dependency_id` + UNIQUE pair |
| `environment` | Development / Staging / Production | `environment_id` |
| `deployment` | a version released into an environment | `deployment_id` + natural key |
| `incident` | an operational failure | `incident_id` |
| `incident_component` | bridge: what each incident affected, and how | composite |

Full DDL: [`database/schema/schema.sql`](database/schema/schema.sql) ·
Design rationale: [`docs/design/design.md`](docs/design/design.md)

## 10. Normalisation

**BCNF** — every determinant is a candidate key.

The full working — functional dependencies written out per table, each normal
form tested against them, the anomalies prevented, and the two places where
further normalisation was deliberately *not* applied — is in
**[`docs/database/normalization.md`](docs/database/normalization.md)**.

The decision that matters most is 1NF. A `dependencies` text column on
`component` holding `"Payment DB, Redis Cache, Kafka"` would kill referential
integrity, joins, indexing, edge attributes and — above all — recursive
traversal. Blast-radius analysis exists *because* dependencies are rows.

## 11. SQL Capabilities

41 queries in 8 files, each stating the business question it answers.

| File | Queries | Focus |
|---|---|---|
| [`01_basic_analysis.sql`](database/queries/01_basic_analysis.sql) | 4 | inventory, catalogue, environments, team roster |
| [`02_ownership_analysis.sql`](database/queries/02_ownership_analysis.sql) | 4 | ownership, unowned components, coverage gaps |
| [`03_dependency_analysis.sql`](database/queries/03_dependency_analysis.sql) | 5 | direct edges, cross-team edges, graph roots/leaves |
| [`04_incident_analysis.sql`](database/queries/04_incident_analysis.sql) | 6 | severity, MTTR, repeat offenders, trend, on-call load |
| [`05_deployment_analysis.sql`](database/queries/05_deployment_analysis.sql) | 5 | production state, health, release timeline, pipeline |
| [`06_risk_analysis.sql`](database/queries/06_risk_analysis.sql) | 5 | risk ranking, single points of failure, change risk |
| [`07_blast_radius.sql`](database/queries/07_blast_radius.sql) | 7 | **recursive** impact analysis |
| [`08_advanced_sql.sql`](database/queries/08_advanced_sql.sql) | 5 | correlation vs causation, window functions, multi-CTE |

**Constructs demonstrated:** `INNER`/`LEFT JOIN`, self join, `GROUP BY`,
`HAVING`, aggregates, scalar and correlated subqueries, `EXISTS` / `NOT EXISTS`,
`CASE`, `UNION ALL`, CTEs, **recursive CTEs**, and window functions (`RANK`,
`DENSE_RANK`, `NTILE`, `PERCENT_RANK`, `LAG`, `LEAD`, `ROW_NUMBER`,
`FIRST_VALUE`, `SUM`/`AVG OVER`, `PARTITION BY`, moving averages), plus the
NULL-safe operator `<=>`.

## 12. Blast-Radius Analysis

```sql
WITH RECURSIVE impact (component_id, depth) AS (
    SELECT component_id, 0                      -- anchor: the failed component
    FROM component WHERE component_name = 'Payment DB'
    UNION                                        -- dedups; guarantees termination
    SELECT d.component_id, im.depth + 1          -- step: whoever depends on the set
    FROM dependency d
    JOIN impact im ON d.depends_on_id = im.component_id
    WHERE im.depth < 20
)
SELECT ...
```

Supports direct dependents, multi-level dependents, propagation **paths**,
affected applications, affected teams, ranking by downstream count, and chain
depth.

> ### ⚠️ What this is, and is not
>
> This is **dependency-based potential impact**. It reports components that have
> a dependency path to the failure.
>
> It does **not** model redundancy, replication, failover, graceful degradation,
> circuit breakers, retries or live health — **none of those are recorded in the
> schema, so none can be computed.** Asynchronous dependencies are currently
> weighted the same as synchronous ones.
>
> Read the output as *"these components have a dependency path to the failure"*,
> not *"these components will go down"*. It is an upper bound and a triage aid.
>
> The caveat is returned in the API payload and shown on the dashboard, so it
> cannot be lost downstream. Query R7 takes the first step towards refining it.

**On cycle prevention specifically.** Cycles are rejected at any length *up to
the configured recursion limit* — MySQL's `cte_max_recursion_depth`, which
defaults to 1000. That is not literally unlimited, and the documentation says so
rather than claiming more. The traversal uses `UNION`, which de-duplicates, so
it terminates after at most N steps for N components; the limit is a safety net,
not the stopping condition. With the default it covers any estate of fewer than
1000 components.

An audit found an earlier 50-hop bound silently accepting a 60-node cycle.
[`tests/sql/06_cycle_regression.sql`](tests/sql/06_cycle_regression.sql) now
builds that exact scenario and guards against it returning.

## 13. Transactions & Concurrency

Six runnable demos in [`database/transactions/`](database/transactions/),
**verified with genuinely concurrent connections** — not described
hypothetically.

| Demonstrated | Measured result |
|---|---|
| Atomicity | a two-write unit where the second fails → *neither* survives |
| `SAVEPOINT` | partial rollback keeps the first edge, discards the second |
| Non-repeatable read (READ COMMITTED) | A reads `High`, B commits, A re-reads → **`Critical`** |
| Repeatable read | same sequence → **`High`**; `Critical` only after A commits |
| Dirty read (READ UNCOMMITTED) | B reads a value A then rolled back |
| Phantom rows | A counts 5, B inserts, A still counts **5** |
| **Lost update** | two responders, both commit, one finding **destroyed** |
| Cure: `FOR UPDATE` | B blocks, then reads A's value — **both findings survive** |
| Row-level locking | different rows never block; the same row does |
| MVCC | a plain `SELECT` is never blocked by an uncommitted write |
| Lock wait timeout | `ERROR 1205` |
| `NOWAIT` / `SKIP LOCKED` | `ERROR 3572` / returns only unlocked rows |
| **Deadlock** | `ERROR 1213`, `lock_deadlocks` 1 → 2, full InnoDB post-mortem |
| Deadlock fix | consistent lock ordering — no deadlock; plus a retry pattern |

```bash
cd database
mysql -u root -p infratrace < transactions/00_demo_setup.sql
sh transactions/run_concurrency_demos.sh          # drives two real connections
mysql -u root -p --table infratrace < transactions/99_demo_teardown.sql
```

The teardown **proves** the seeded dataset is unchanged. Full discussion:
[`docs/database/transactions.md`](docs/database/transactions.md)

## 14. Query Optimisation

At 19 components MySQL correctly ignores every index, so a "before/after" would
show nothing. All measurements therefore run on a **separate generated schema**
(2,000 components, 7,878 edges, 40,000 deployments).

| Access pattern | Before | After | Gain |
|---|---|---|---|
| Reverse dependency lookup | 0.914 ms `type=ALL` | 0.013 ms covering index | **~70×** |
| Latest deployment per component/env | 7.24 ms `ALL` + filesort | 0.020 ms `ref` | **~350×** |
| Incident severity + status | 1.15 ms `ALL` | 0.089 ms `ref` | **~13×** |
| Recursive blast radius | 8.0 ms | 3.5 ms | ~2.3× |

**Two findings that contradict intuition:**

- An index that looked obviously right — `incident_component(component_id,
  incident_id)` — made the optimiser's cost estimate **9× better** and the query
  **2.8× slower** (39 → 108 ms). It was **rejected on measured evidence**.
- Carrying a `depth` column in a recursive CTE made the same traversal **15×
  slower** (54.5 → 3.7 ms), because `UNION` de-duplicates on the *whole row*, so
  a component is re-expanded once per depth it can be reached at.

Full study: [`docs/database/query-optimization.md`](docs/database/query-optimization.md) ·
Index rationale: [`docs/database/indexing.md`](docs/database/indexing.md)

## 15. Backend

FastAPI, **27 read-only endpoints**. Full reference:
[`docs/api/api.md`](docs/api/api.md) · interactive docs at `/docs`.

| Group | Endpoints |
|---|---|
| Components | list, detail, dependencies (`?recursive=`), dependents, **blast-radius**, incidents, deployments, impact-report |
| Organisation | teams, team detail, applications, application components, environments |
| Operations | incidents (+filters), incident detail, deployments (+filters) |
| Analytics | summary, high-risk-components, critical-incidents, team-health, production-infrastructure, unowned-components, incident-trend, deploy-incident-correlation |
| Graph | `/graph`, `/graph/cycles` |

**Security:** parameterised SQL everywhere; stored-procedure names validated
against an allowlist; credentials from environment variables only; a
`SELECT` + `EXECUTE`-only database account; CORS origin allowlist with no
wildcard; FastAPI type and range validation on every parameter; `GET` only.

## 16. Frontend

A single static page — no framework, no build step. Open it and it works.

| View | Shows |
|---|---|
| **Dashboard** | 12 KPIs, highest blast radius, unowned components, team risk, incident trend, deploy→incident correlation |
| **Components** | full catalogue with live filters and risk metrics |
| **Dependency Explorer** | pick a component → dependencies, dependents, blast-radius chain, affected applications, teams to page, propagation paths, **impact subgraph** |
| **Dependency Graph** | the whole graph as layered SVG — colour by criticality, dashed for async, dashed red outline for unowned |
| **Incidents** | severity/status filters, root cause, affected components |
| **Deployments** | live production state plus full history |

Each card names the SQL behind it, so it is obvious the numbers come from the
database. Verified in a headless browser: all views render, no console errors,
filters work, no horizontal overflow at 420px.

## 17. Setup

**Prerequisites:** MySQL 8.0.16+ (CHECK constraints require 8.0.16; the project
is verified on 8.0.46). Python 3.10+ only for the optional API.

Both options below are verified: the full suite has been run end to end on
**MySQL 8.0.46 natively on Windows** and on **MySQL 8.0.46 in a Linux
container**, with identical results.

### Option A — Local MySQL (recommended)

Best for a lab or demo: MySQL Workbench can connect to it, and there is no
container to start.

`SOURCE` resolves paths relative to the client's working directory, so run from
inside `database/`.

```bash
cd database
mysql -u root -p < setup.sql

cd ../tests
mysql -u root -p --table < run_all_tests.sql      # expect: ALL TESTS PASSED
```

<details>
<summary>Optional: avoid retyping the password (login-path)</summary>

MySQL can store credentials in an obfuscated file, so no password appears on any
command line or in shell history:

```bash
mysql_config_editor set --login-path=infratrace --host=127.0.0.1 --port=3306 --user=root --password
```

Then use `mysql --login-path=infratrace` in place of `mysql -u root -p`
everywhere in this README. This is also the cleanest way to run the concurrency
demonstrations, which open several connections.

On Windows the client is not on `PATH` by default; it lives at
`C:\Program Files\MySQL\MySQL Server 8.0\bin\`.
</details>

<details>
<summary>MySQL Workbench, or step by step</summary>

Run in this order: `schema/schema.sql` → `data/seed.sql` → `views/views.sql` →
`functions/functions.sql` → `procedures/procedures.sql` → `triggers/triggers.sql`.

Order matters: the dependency triggers call `fn_would_create_cycle`, so
functions must exist before triggers.
</details>

### Option B — Docker MySQL (fallback / reproducibility)

Useful if you would rather not install MySQL, or want a disposable instance.

```bash
docker run -d --name infratrace-mysql -p 3307:3306 \
       -e MYSQL_ROOT_PASSWORD=<choose-one> mysql:8.0

# wait for it to accept connections, then:
docker cp database infratrace-mysql:/infratrace/database
docker cp tests    infratrace-mysql:/infratrace/tests
docker exec infratrace-mysql sh -c 'cd /infratrace/database && mysql -u root -p<same> < setup.sql'
docker exec infratrace-mysql sh -c 'cd /infratrace/tests && mysql -u root -p<same> --table < run_all_tests.sql'
```

Port **3307** is used so it cannot clash with a local MySQL on 3306. Copy the
project into `/infratrace/` so that `database/` and `tests/` stay siblings —
the suite uses a relative `SOURCE ../database/...` path.

### A note on character sets

Every SQL entry point begins with:

```sql
SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci;
```

This is not decoration. The `mysql` client derives its connection character set
from the **host platform** — `cp850` on a Windows console, `latin1` on Linux —
and neither matches this `utf8mb4_unicode_ci` database. Without the directive,
expressions mixing a SQL literal with column data raise
`ERROR 1271 Illegal mix of collations` on Windows while working on Linux. The
`COLLATE` clause matters too: the MySQL 8 server default is
`utf8mb4_0900_ai_ci`, so naming only the charset would still leave literals and
`CAST(... AS CHAR)` results in a different collation from the columns.

### Backend (optional)

```bash
cd backend
python -m pip install -r requirements.txt
cp .env.example .env          # then fill in DB_USER / DB_PASSWORD
python -m uvicorn app.main:app --reload
```

Create a least-privilege account first:

```sql
CREATE USER 'infratrace_api'@'localhost' IDENTIFIED BY 'choose-a-strong-password';
GRANT SELECT  ON infratrace.* TO 'infratrace_api'@'localhost';
GRANT EXECUTE ON infratrace.* TO 'infratrace_api'@'localhost';
FLUSH PRIVILEGES;
```

`EXECUTE` is needed because the API calls stored procedures and functions. No
write grant is required — or wanted.

### Frontend (optional)

```bash
cd frontend
python -m http.server 8080
# open http://127.0.0.1:8080
```

It expects the API at `http://127.0.0.1:8000`. To point elsewhere, in the
browser console:
`localStorage.setItem('infratrace_api','http://host:port'); location.reload()`

## 18. Testing

```bash
cd tests   && mysql -u root -p --table < run_all_tests.sql     # 166 tests
cd backend && python -m pytest tests/ -v                       # 45 tests
```

| Suite | Tests | Proves |
|---|---|---|
| 01 schema | 37 | every object exists, by name; no function falsely `DETERMINISTIC` |
| 02 constraints | 25 | 21 invalid writes are actually **rejected**, and the data is unchanged |
| 03 integrity | 28 | row counts, one root cause per incident, **graph acyclic**, edge cases present |
| 04 stored programs | 34 | hand-derived values; views never multiply rows; rollback honoured |
| 05 analytics | 22 | analysis non-empty and correct; **fan-out guards**; demo scenarios still exist |
| 06 cycle regression | 20 | cycles at 1, 2, 3, 4, 35 and **60** nodes rejected; detector finds an injected cycle |
| API | 45 | endpoints, blast-radius chain, caveat present, injection resistance, JSON types |

Details and known gaps: [`docs/testing/testing.md`](docs/testing/testing.md)

## 19. Example Queries

```sql
USE infratrace;

-- What breaks if Payment DB fails, and who do we page?
CALL sp_get_blast_radius(11);

-- Everything about one component in one call (five result sets)
CALL sp_component_impact_report(11);

-- Is the dependency graph acyclic?  (no rows = yes)
CALL sp_detect_dependency_cycles();

-- Risk metrics for any component
SELECT fn_blast_radius_count(16), fn_risk_score(16), fn_dependency_depth(1);

-- What is running in production right now?
SELECT * FROM production_infrastructure_view;

-- Which components have no owner?
SELECT * FROM component_ownership_view WHERE owning_team = 'UNASSIGNED';
```

```bash
# Whole category files
cd database
mysql -u root -p --table infratrace < queries/07_blast_radius.sql
mysql -u root -p --table infratrace < queries/06_risk_analysis.sql

# Every DBMS feature, including 8 deliberate constraint violations
mysql -u root -p --force --table infratrace < queries/09_dbms_features_demo.sql
```

## 20. Demo Walkthrough

A 15-minute run-through:

| # | Step | Command |
|---|---|---|
| 1 | Problem statement | `docs/requirements/requirements.md` |
| 2 | ER diagram | `docs/diagrams/er-diagram.md` (preview the Mermaid) |
| 3 | Relational schema | `database/schema/schema.sql` |
| 4 | Real tables | `SHOW TABLES;` then `SHOW CREATE TABLE dependency\G` |
| 5 | Realistic records | `SELECT * FROM component_ownership_view;` |
| 6 | Largest blast radius | `queries/06_risk_analysis.sql` (K1) |
| 7 | **Blast radius of Payment DB** | `CALL sp_get_blast_radius(11);` |
| 8 | The chain | `Payment DB → Payment Service → Order Service → API Gateway` |
| 9 | Applications and teams affected | `CALL sp_component_impact_report(11);` |
| 10 | Related incidents | section 5 of the same report |
| 11 | Deployment history | `queries/05_deployment_analysis.sql` |
| 12 | **Transactions** | `sh transactions/run_concurrency_demos.sh` |
| 13 | **EXPLAIN and indexes** | `performance/02_explain_analysis.sql` |
| 14 | Constraints refusing bad data | `queries/09_dbms_features_demo.sql --force` |
| 15 | Dashboard | `http://127.0.0.1:8080` → Dependency Explorer → Payment DB |

That path shows **database → SQL → business analysis → application**.

## 21. Limitations

Stated plainly, because over-claiming is worse than a shorter feature list.

1. **Blast radius is an upper bound.** No redundancy, failover, graceful
   degradation, circuit breakers or live health — none are in the schema.
   Asynchronous dependencies are weighted like synchronous ones.
2. **Risk score is a heuristic**, not a probability. It ranks; the absolute
   number has no unit.
3. **Cycle prevention on `UPDATE` is conservative.** The check runs before the
   update applies, so re-pointing an edge in place can be refused even when the
   result would be acyclic. Erring towards refusal is the safe direction.
4. **Recursion is capped at 20 levels.**
5. **Correlation ≠ causation.** The 7-day window is a suspicion; only
   `caused_by_deployment_id` is a conclusion. Never merged.
6. **Performance numbers come from a generated dataset**, because the real one is
   too small for the optimiser to use an index. Stated wherever quoted.
7. **No authentication, no write endpoints, no CI.** The API is localhost-only.
8. **Durability is not demonstrated.** Doing it honestly means killing the server
   mid-transaction.

## 22. Future Scope

| Priority | Extension |
|---|---|
| Next | Weight blast radius by `dependency_type` — the data is already recorded |
| Next | Model redundancy and failover, so a replicated database stops being a single point of failure |
| Next | Write endpoints with authentication |
| Later | Historical versioning of the graph — "what did the architecture look like *during* that incident?" |
| Later | CI running both suites against a MySQL service container |
| Later | Import dependencies from service meshes or CI/CD metadata |
| Optional | AI **on top of** the database: incident correlation, anomaly detection, natural-language querying |

Every item adds to the database. Nothing above is implemented, and nothing above
is claimed to be.

## 23. Project Structure

```
InfraTrace/
├── database/
│   ├── setup.sql                     # one command builds everything
│   ├── schema/schema.sql             # 10 tables, constraints, indexes
│   ├── data/seed.sql                 # ShopSphere dataset, 220 rows
│   ├── views/views.sql               # 4 views
│   ├── functions/functions.sql       # 6 stored functions
│   ├── procedures/procedures.sql     # 5 stored procedures
│   ├── triggers/triggers.sql         # 5 triggers
│   ├── queries/                      # 01..08 analytical + 09 feature demo
│   ├── transactions/                 # ACID, isolation, locking, deadlock
│   └── performance/                  # load generator + EXPLAIN study
├── backend/
│   ├── app/{main,db,queries}.py      # FastAPI, data layer, all SQL
│   ├── tests/test_api.py             # 45 integration tests
│   ├── requirements.txt
│   └── .env.example
├── frontend/index.html               # dashboard, no build step
├── docs/
│   ├── requirements/ design/ diagrams/
│   ├── database/  normalization · indexing · transactions ·
│   │              query-optimization · dbms-concepts
│   ├── api/api.md
│   └── testing/testing.md
├── tests/
│   ├── run_all_tests.sql
│   └── sql/00..06, 99_summary.sql    # 166 tests
├── README.md
└── .gitignore
```

**Start here:** [`docs/database/dbms-concepts.md`](docs/database/dbms-concepts.md)
maps every DBMS syllabus topic to the file that implements it and the command
that demonstrates it.

## 24. Team Contribution

> **TODO before submission** — the names and registration numbers below are
> placeholders. The work areas are listed so they can be assigned; fill in the
> first two columns before handing this in.

| Name | Registration number | Work area |
|---|---|---|
| *TODO* | *TODO* | Schema design, normalisation, constraints |
| *TODO* | *TODO* | Analytical SQL, recursive queries, blast radius |
| *TODO* | *TODO* | Stored programs, triggers, transactions |
| *TODO* | *TODO* | Backend API, dashboard, testing |
| *TODO* | *TODO* | Documentation, performance study |

## 25. License

Released under the **MIT License** — see [`LICENSE`](LICENSE).

The ShopSphere dataset is fictional. Company, team, service and incident names
were invented for this project and do not describe any real organisation or
system.

---

**Course:** Database Management Systems · **DBMS:** MySQL 8.0.46 (InnoDB)
**Verified on:** Windows 11 (native MySQL 8.0.46) and Linux (MySQL 8.0.46 container)
