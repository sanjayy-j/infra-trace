# InfraTrace — Database Design

**DBMS:** MySQL 8.0+ (InnoDB)
**Character set:** `utf8mb4` / `utf8mb4_unicode_ci`

---

## 1. Entities

| # | Entity | Description | Primary Key |
|---|--------|-------------|-------------|
| 1 | `team` | An engineering team that owns applications and components. | `team_id` |
| 2 | `developer` | An engineer; belongs to at most one team. | `developer_id` |
| 3 | `application` | A customer-facing or internal product. | `application_id` |
| 4 | `component` | Any service, database, cache, queue, storage system, gateway or external API. **The central entity.** | `component_id` |
| 5 | `application_component` | Bridge: which components an application uses. | `(application_id, component_id)` |
| 6 | `dependency` | Bridge: which component depends on which other component. **The dependency graph.** | `dependency_id` |
| 7 | `environment` | Development / Staging / Production. | `environment_id` |
| 8 | `deployment` | A release of a component version into an environment. | `deployment_id` |
| 9 | `incident` | An operational failure with severity and timeline. | `incident_id` |
| 10 | `incident_component` | Bridge: which components an incident affected, and how. | `(incident_id, component_id)` |

## 2. Relationships

| Relationship | Type | Implementation |
|--------------|------|----------------|
| team — developer | 1 : N | `developer.team_id` → `team.team_id` |
| team — application | 1 : N | `application.owner_team_id` → `team.team_id` |
| team — component | 1 : N | `component.owner_team_id` → `team.team_id` |
| application — component | **M : N** | `application_component` bridge table |
| **component — component** | **M : N, self-referencing** | `dependency` bridge table |
| component — deployment | 1 : N | `deployment.component_id` → `component.component_id` |
| environment — deployment | 1 : N | `deployment.environment_id` → `environment.environment_id` |
| developer — deployment | 1 : N | `deployment.deployed_by` → `developer.developer_id` |
| environment — incident | 1 : N | `incident.environment_id` → `environment.environment_id` |
| developer — incident | 1 : N | `incident.reported_by` → `developer.developer_id` |
| incident — component | **M : N** | `incident_component` bridge table |

### The self-referencing dependency relationship

This is the heart of the design. A component both *has* dependencies and *is* a
dependency of others, so the relationship is many-to-many from `component` back
to `component`:

```
component (component_id)  ──┐
                            ├──►  dependency (component_id, depends_on_id)
component (component_id)  ──┘
```

Reading direction — `Order Service → Payment Service`:

| Column | Value | Meaning |
|--------|-------|---------|
| `component_id` | Order Service | the dependent (upstream caller) |
| `depends_on_id` | Payment Service | the dependency (downstream provider) |

Two consequences follow, and they are what make the whole project work:

- **"What does X need?"** — filter on `component_id = X`, follow `depends_on_id`.
- **"What breaks if X fails?"** — filter on `depends_on_id = X`, follow
  `component_id`. Repeating this step recursively produces the blast radius.

Component details are stored exactly once, in `component`. The `dependency`
table stores only the two foreign keys and the attributes *of the edge itself*
(`dependency_type`, `is_critical`, `description`).

## 3. Relational Schema

Primary keys are **underlined** conceptually and marked `PK`; foreign keys `FK`.

```
team(team_id PK, team_name UNIQUE NOT NULL, team_email UNIQUE NOT NULL,
     created_on NOT NULL)

developer(developer_id PK, developer_name NOT NULL, email UNIQUE NOT NULL,
          job_role NOT NULL, team_id FK→team NULL, joined_on NOT NULL)

application(application_id PK, application_name UNIQUE NOT NULL,
            app_type NOT NULL CHECK, owner_team_id FK→team NULL, description)

component(component_id PK, component_name UNIQUE NOT NULL,
          component_type NOT NULL CHECK, owner_team_id FK→team NULL,
          criticality NOT NULL CHECK, tech_stack,
          is_active NOT NULL CHECK, created_on NOT NULL)

application_component(application_id FK→application, component_id FK→component,
                      usage_notes,
                      PK(application_id, component_id))

dependency(dependency_id PK,
           component_id  FK→component NOT NULL,
           depends_on_id FK→component NOT NULL,
           dependency_type NOT NULL CHECK, is_critical NOT NULL CHECK,
           description,
           UNIQUE(component_id, depends_on_id))
           -- component_id <> depends_on_id is enforced by a trigger,
           -- not a CHECK. See design decision 5 below.

environment(environment_id PK, environment_name UNIQUE NOT NULL,
            region NOT NULL, is_production NOT NULL CHECK)

deployment(deployment_id PK, component_id FK→component NOT NULL,
           environment_id FK→environment NOT NULL, version NOT NULL,
           deployed_by FK→developer NULL, deployed_at NOT NULL,
           status NOT NULL CHECK ('Success','Failed'),
           is_rollback NOT NULL CHECK,
           UNIQUE(component_id, environment_id, deployed_at))

incident(incident_id PK, title NOT NULL, severity NOT NULL CHECK,
         status NOT NULL CHECK, environment_id FK→environment NOT NULL,
         reported_by FK→developer NULL, started_at NOT NULL, resolved_at NULL,
         root_cause, caused_by_deployment_id FK→deployment NULL, updated_at,
         CHECK(resolved_at IS NULL OR resolved_at >= started_at),
         CHECK(status <> 'Resolved' OR resolved_at IS NOT NULL))

incident_component(incident_id FK→incident, component_id FK→component,
                   impact_level NOT NULL CHECK,
                   PK(incident_id, component_id))
```

## 4. Normalisation

The schema is in **Boyce-Codd Normal Form (BCNF)** — every determinant is a
candidate key, which is strictly stronger than the 3NF usually required.

The summary below covers the reasoning; the full working — functional
dependencies written out per table, each normal form tested against them, and
the two places where further normalisation was deliberately *not* applied — is
in [`docs/database/normalization.md`](../database/normalization.md).

**1NF** — every attribute holds a single atomic value. There are no repeating
groups. The obvious temptation, a `dependencies` text column on `component`
holding a comma-separated list, is rejected: it would be unqueryable and
untraversable. Those facts live in `dependency`, one row per edge. The same
reasoning applies to an application's component list and an incident's affected
components.

**2NF** — 1NF, and every non-key attribute depends on the *whole* primary key.
The three tables with composite keys are checked explicitly:

- `application_component(application_id, component_id)` — `usage_notes`
  describes how *that* application uses *that* component, so it depends on both.
- `incident_component(incident_id, component_id)` — `impact_level` describes how
  *that* incident affected *that* component, so it depends on both.
- Every other table has a single-column surrogate key, so 2NF holds trivially.

**3NF** — 2NF, and no non-key attribute depends on another non-key attribute.
The important cases:

- `component` stores `owner_team_id`, never `team_name` or `team_email`. Storing
  the name would make it transitively dependent on `component_id` through
  `owner_team_id`, and renaming a team would require updating many rows.
- `deployment` stores `component_id` and `environment_id`, never
  `component_name` or `environment_name`.
- `incident` stores `environment_id` and `reported_by`, never the environment
  name or the reporter's email.
- `developer` stores `team_id`, never the team's email.

Human-readable names are reassembled at query time by joining, or by the views
in `database/views/views.sql`, which exist precisely so that denormalisation is
never needed for convenience.

**BCNF** — every determinant listed above is a candidate key. The relation
worth checking is `dependency`, which has two: the surrogate `dependency_id` and
the natural pair `(component_id, depends_on_id)` enforced by `uq_dependency_edge`.
Both are candidate keys, so no determinant is a non-key attribute.

**Note on `is_production`.** `environment.is_production` is a genuine attribute
of an environment, functionally dependent on `environment_id` alone — not on
`environment_name`. It exists so queries filter on a semantic flag rather than
on the string `'Production'`, which keeps them correct if a second production
region is added later.

## 5. Constraints Used

| Constraint type | Where |
|---|---|
| PRIMARY KEY | All 10 tables (2 composite, 8 surrogate) |
| FOREIGN KEY | 14 foreign keys with explicit `ON UPDATE` / `ON DELETE` behaviour |
| UNIQUE | `team_name`, `team_email`, `developer.email`, `application_name`, `component_name`, `environment_name`, `dependency(component_id, depends_on_id)` |
| NOT NULL | All identifying and business-required attributes |
| CHECK | Enumerated values (`component_type`, `criticality`, `app_type`, `dependency_type`, `deployment.status`, `incident.severity`, `incident.status`, `impact_level`), boolean flags, self-dependency, and incident timeline validity |
| DEFAULT | `criticality`, `is_active`, `dependency_type`, `is_critical`, `deployment.status`, `incident.status`, `updated_at` |

### Referential actions and why

| Foreign key | On delete | Reason |
|---|---|---|
| `developer.team_id` → team | `SET NULL` | Deleting a team must not delete its engineers; they become unassigned. |
| `component.owner_team_id` → team | `SET NULL` | Same: infrastructure survives a reorganisation, and becomes visibly unowned. |
| `application.owner_team_id` → team | `SET NULL` | As above. |
| `dependency.*` → component | `CASCADE` | An edge is meaningless once either endpoint is gone. |
| `application_component.*` | `CASCADE` | A link row is meaningless without both sides. |
| `incident_component.*` | `CASCADE` | As above. |
| `deployment.component_id` → component | `CASCADE` | Deployment history belongs to the component. |
| `deployment.environment_id` → environment | `RESTRICT` | An environment with deployment history must not be silently deleted. |
| `incident.environment_id` → environment | `RESTRICT` | Incident history must retain the environment it happened in. |
| `deployment.deployed_by`, `incident.reported_by` → developer | `SET NULL` | History outlives employment; the record survives, the attribution is dropped. |

## 6. Important Design Decisions

**1. `owner_team_id` is nullable on `component`.**
This is deliberate, not an oversight. An unowned component is a real and
dangerous state — when it fails, nobody is paged. Making the column `NOT NULL`
would force a fake owner and hide the problem. Two components in the sample data
(`Shipping Service`, `Legacy Coupon Service`) have no owner, and query Q11
surfaces them along with their blast radius.

**2. `dependency` uses a surrogate key plus a unique constraint.**
The natural key is `(component_id, depends_on_id)`, and it is enforced with
`UNIQUE`. A surrogate `dependency_id` is kept as the primary key so an edge can
be referenced by a single value in later phases (for example, dependency change
history), without a composite foreign key.

**3. Enumerations are `VARCHAR` + `CHECK`, not `ENUM`.**
MySQL's `ENUM` is not portable to other SQL dialects and adding a value requires
`ALTER TABLE` on the column definition. `VARCHAR` with a `CHECK` constraint gives
the same validation, is standard SQL, and reads clearly in the schema.

**4. `is_critical` on the edge, `criticality` on the node.**
These answer different questions. `component.criticality` is how important the
component is in itself; `dependency.is_critical` is whether *this particular
call path* is on the critical path. Payment Service depends critically on
Payment DB but only optionally on Redis Cache, even though both are important
components.

**5. Some rules must be triggers, not CHECK constraints.**
Two separate MySQL limitations push validation into `triggers.sql`:

- *A CHECK constraint may only inspect the row it is defined on.* The mutual
  cycle rule, the retired-component-in-production rule, and the incident
  status/timestamp rule all need to read other rows or other tables, so none of
  them can be a CHECK constraint.
- *A column used in a CHECK constraint may not also carry a foreign-key
  referential action.* The self-dependency rule, `component_id <> depends_on_id`,
  looks like a textbook CHECK constraint and inspects only one row — but MySQL 8
  rejects it with error 3823, because both columns are foreign keys with
  `ON UPDATE CASCADE` / `ON DELETE CASCADE`. Cascading deletes of dependency
  edges are worth keeping, so the rule moved to
  `trg_dependency_validate_insert` / `trg_dependency_validate_update`, which also
  covers `UPDATE` — something a CHECK would have handled automatically.

CHECK constraints are still used wherever they work: all eight enumerated
columns, the boolean flags, and the incident timeline rules.

**6. Indexes go beyond the automatic ones.**
InnoDB creates an index on every foreign key column automatically, so
single-column indexes on `dependency.component_id`, `deployment.component_id`
and so on already exist. The indexes added explicitly are the ones that add
something further — notably `idx_dependency_reverse (depends_on_id,
component_id)`, which covers the reverse lookup that blast-radius recursion
performs at every level of the traversal.

**7. Recency is measured against the data, not the clock.**
Analytical queries that ask about "recent" activity compare against
`(SELECT MAX(started_at) FROM incident)` rather than `CURDATE()`. A fixed sample
dataset compared against a moving system clock would silently start returning
empty results. This keeps every query meaningful whenever the database is loaded.

**8. `status` and `is_rollback` are separate columns on `deployment`.**
The original design had a single status with values `Success`, `Failed` and
`RolledBack`, and that conflated two different facts: *"this deployment was
later rolled back"* and *"this deployment IS a rollback"*. The consequence was a
real bug — `production_infrastructure_view` reported Inventory Service running
`v4.0.0`, the version that had been rolled back, instead of the `v3.9.0` that
was actually live.

The fix splits the ideas:

- `status` describes the OUTCOME of this deployment attempt: `Success` or `Failed`.
- `is_rollback` says whether it put an EARLIER version back.

"What is running now" is then simply the latest row with `status = 'Success'`,
whether or not that row was a rollback. A regression test asserts the view
reports `v3.9.0`.

**9. `incident.caused_by_deployment_id` separates causation from correlation.**
A time-based query can show that a deployment happened shortly before an
incident. That is a *suspicion*. This nullable foreign key records a
*conclusion*, set only when a post-incident review has confirmed the cause.

Keeping them in separate columns stops the analysis overstating itself: 21 time
correlations exist in the dataset, but only 12 incidents have a confirmed cause.
Reporting all 21 as "caused by" would be wrong, and query A1 in
`08_advanced_sql.sql` labels every row so the distinction cannot be lost.

**10. Cycle prevention is a trigger calling a reachability function.**
`fn_would_create_cycle(A, B)` answers "would adding A → B close a loop?" by
walking the graph downwards from B and checking whether it arrives back at A.
The dependency triggers call it on every INSERT and UPDATE, so a cycle of ANY
length is rejected — not just self-dependency and the direct mutual pair the
first implementation caught.

`sp_detect_dependency_cycles()` remains as the audit, because triggers can be
bypassed by a bulk load or a restored dump. Prevention and detection answer
different questions and both are needed.

**11. `production_infrastructure_view` does not filter `is_active`.**
This is a modelling decision, not an oversight. `component.is_active` is a
catalogue state; `deployment` rows are history. The schema has no undeploy or
decommission event, so marking a component retired does not stop it running.
The view answers "what is running", so a component retired after deployment
still appears — and that is the useful answer, because it is something still
running that nobody owns any more.

The complementary rule *is* enforced, in the opposite direction: a retired
component can never acquire a new production deployment
(`trg_deployment_validate_insert` / `_update`). Requirement FR23 asks for
exactly that, and asks nothing about hiding existing deployments.

If a future phase adds a decommission event, the view should filter on that
event rather than on `is_active`.

## 7. Limitations of the Current Blast-Radius Model

Stated explicitly so the analysis is not over-claimed:

- Every dependency is treated as total. A component that depends on a failed
  component is reported as affected, even if it would in practice degrade
  gracefully.
- Asynchronous dependencies are weighted the same as synchronous ones, although
  a queue-based consumer usually tolerates a downstream outage for longer.
- Redundancy and failover are not modelled; a replicated database is treated as
  a single point of failure.
- The recursion depth is capped at 20 levels as a safety stop.

R7 in `07_blast_radius.sql` takes the first step towards fixing the first two
points: it restricts the traversal to `is_critical = 1` edges and shows the gap
between the full and critical-path blast radius — which is exactly the amount
the current model over-reports.

Refining these is the first item of Phase 2 in the requirements document.
