# InfraTrace — DBMS Concepts Mapping

How this project demonstrates each concept from the Database Management Systems
syllabus, with a pointer to the file where it is implemented and the command
that shows it working.

Everything below has been executed against **MySQL 8.0.46**. Where a number is
quoted, it is a measured result, not an estimate.

---

## 1. The Relational Model

InfraTrace stores infrastructure knowledge as **relations** (tables) whose rows
are tuples over typed, atomic attributes. Nothing is stored as a document, a
blob, or a delimited list.

| Relational concept | In InfraTrace |
|---|---|
| Relation | `component`, `dependency`, `incident`, … (10 tables) |
| Tuple | one component; one dependency edge; one deployment event |
| Attribute | `component.criticality`, `dependency.is_critical` |
| Domain | enforced by data type **plus** a `CHECK` constraint |
| Relation schema | `component(component_id, component_name, component_type, …)` |
| Key | `component_id` (primary), `component_name` (candidate) |

The important claim for this project is that a **graph** — usually presented as
the thing relational databases are bad at — is represented entirely inside the
relational model, as a relation over two foreign keys, and is then queried with
standard SQL.

**Relational algebra behind the headline query.** "What depends on Payment DB"
is a selection followed by a projection over a join:

```
π component_name ( σ depends_on_id = 11 (dependency) ⋈ component )
```

and blast radius is the **transitive closure** of that relation, which is what
`WITH RECURSIVE` computes.

---

## 2. ER Modelling

Full diagram: [`docs/diagrams/er-diagram.md`](../diagrams/er-diagram.md)

| ER construct | Example | Mapped to |
|---|---|---|
| Strong entity | Team, Component, Incident | its own table with a surrogate key |
| Relationship 1:N | Team **owns** Component | FK `component.owner_team_id` |
| Relationship M:N | Application **uses** Component | bridge table `application_component` |
| **Recursive (unary) relationship** | Component **depends on** Component | bridge table `dependency`, two FKs to the same table |
| Attribute on a relationship | *how* an application uses a component | `application_component.usage_notes` |
| | *how severely* an incident hit a component | `incident_component.impact_level` |
| | *what kind* of dependency an edge is | `dependency.dependency_type`, `is_critical` |
| Optional participation | a component need not have an owner | `owner_team_id` nullable |
| Total participation | a deployment must have an environment | `environment_id NOT NULL` |

The recursive relationship is the design's centre of gravity. Because both
`dependency.component_id` and `dependency.depends_on_id` reference
`component.component_id`, one table describes the entire dependency graph, and
component details are never duplicated.

---

## 3. Functional Dependencies

Writing the FDs out is what justifies the normal form claim.

**`team`**
```
team_id  → team_name, team_email, created_on
team_name  → team_id          (candidate key)
team_email → team_id          (candidate key)
```

**`component`**
```
component_id   → component_name, component_type, owner_team_id,
                 criticality, tech_stack, is_active, created_on
component_name → component_id   (candidate key)
```
No non-key attribute determines another. In particular `owner_team_id` does
**not** determine `criticality` — two components owned by the same team can
have different criticality — so there is no transitive dependency to remove.

**`dependency`**
```
dependency_id                 → component_id, depends_on_id, dependency_type,
                                is_critical, description
(component_id, depends_on_id) → dependency_id, dependency_type,
                                is_critical, description
```
The composite is the natural key and is enforced by `uq_dependency_edge`;
`dependency_id` is a surrogate kept so an edge can be referenced by one value.

**`application_component`** — composite key, and the non-key attribute depends
on *both* halves:
```
(application_id, component_id) → usage_notes
```
`usage_notes` describes how *that* application uses *that* component. Neither
`application_id → usage_notes` nor `component_id → usage_notes` holds, which is
exactly the test for 2NF.

**`incident_component`**
```
(incident_id, component_id) → impact_level
```
Same reasoning: a component is `RootCause` in one incident and `Degraded` in
another, so `impact_level` depends on the whole key.

**`deployment`**
```
deployment_id                              → component_id, environment_id, version,
                                             deployed_by, deployed_at, status, is_rollback
(component_id, environment_id, deployed_at) → deployment_id, version, status, …
```
The second FD is the natural key, enforced by `uq_deployment_event` — one
component cannot be deployed into the same environment at the same instant.

**`incident`**
```
incident_id → title, severity, status, environment_id, reported_by,
              started_at, resolved_at, root_cause, caused_by_deployment_id
```
Note what is deliberately **not** an FD: `status → resolved_at`. A resolved
incident must have a timestamp (a `CHECK` enforces it), but the status does not
*determine* which timestamp.

---

## 4. Normalisation

### 1NF — atomic values, no repeating groups

Every attribute holds one value. The tempting shortcut — a `dependencies` text
column on `component` holding `"Payment DB, Redis Cache, Kafka"` — is rejected,
because it cannot be joined, cannot be indexed, cannot be constrained by a
foreign key, and above all cannot be **traversed recursively**. The entire
blast-radius feature exists because dependencies are rows.

The same reasoning rejects a comma-separated component list on `application`
and on `incident`.

### 2NF — no partial dependency on part of a composite key

Only three tables have composite keys, and each is checked explicitly above:
`application_component`, `incident_component`, and the natural key of
`deployment`. Every other table has a single-attribute key, so 2NF is
automatic.

### 3NF — no transitive dependency through a non-key attribute

The violations avoided:

- `component` stores `owner_team_id`, never `team_name`. Storing the name would
  create `component_id → owner_team_id → team_name`, and renaming a team would
  require updating many component rows (update anomaly).
- `deployment` stores ids, never `component_name` or `environment_name`.
- `incident` stores `environment_id` and `reported_by`, never the environment
  name or the reporter's email address.

Readable names are reassembled by joining, or by the **views** — which exist
precisely so that nobody is tempted to denormalise for convenience.

### BCNF

Every determinant listed in section 3 is a candidate key, so the schema is in
**BCNF**, not merely 3NF.

The one case worth examining is `dependency`, which has two determinants:
`dependency_id` and `(component_id, depends_on_id)`. Both are candidate keys —
the surrogate by construction, the pair by the `UNIQUE` constraint — so no
determinant is a non-key attribute and BCNF holds.

A schema in 3NF but *not* BCNF needs an overlapping composite candidate key with
a non-trivial FD from a proper subset. No table here has that shape.

### The anomalies this prevents

| Anomaly | What it would look like | Prevented by |
|---|---|---|
| Update | renaming "Payments Team" requires editing many component rows | `owner_team_id` FK |
| Insert | cannot record a team until it owns a component | `team` is its own relation |
| Delete | deleting the last component of a team erases the team | `team` is its own relation |
| Inconsistency | the same dependency edge recorded twice with different types | `uq_dependency_edge` |

Full discussion: [`normalization.md`](normalization.md)

---

## 5. Constraints

Measured counts on the live database: **14 CHECK, 15 FOREIGN KEY, 8 UNIQUE**.

| Type | Count | Examples |
|---|---|---|
| PRIMARY KEY | 10 | 8 surrogate, 2 composite (`application_component`, `incident_component`) |
| FOREIGN KEY | 15 | each with an explicit `ON UPDATE` / `ON DELETE` action |
| UNIQUE | 8 | `component_name`, `team_email`, `uq_dependency_edge`, `uq_deployment_event` |
| NOT NULL | many | every identifying and business-required attribute |
| CHECK | 14 | 8 enumerations, 4 boolean flags, 2 incident-timeline rules |
| DEFAULT | 7 | `criticality`, `is_active`, `is_rollback`, `status`, `updated_at`, … |

### Referential actions, and why each was chosen

| Foreign key | ON DELETE | Reason |
|---|---|---|
| `component.owner_team_id` → team | `SET NULL` | Infrastructure must survive a reorganisation, and become **visibly** unowned. |
| `developer.team_id` → team | `SET NULL` | Deleting a team must not delete its engineers. |
| `dependency.*` → component | `CASCADE` | An edge is meaningless once an endpoint is gone. |
| `application_component.*` | `CASCADE` | A link row is meaningless without both sides. |
| `incident_component.*` | `CASCADE` | Same. |
| `deployment.environment_id` → environment | `RESTRICT` | An environment with history must not vanish silently. |
| `incident.environment_id` → environment | `RESTRICT` | Same. |
| `deployment.deployed_by` → developer | `SET NULL` | History outlives employment. |
| `incident.caused_by_deployment_id` → deployment | `SET NULL` | The incident survives, losing only its confirmed cause. |

### Two MySQL limitations that shaped the design

1. **A `CHECK` may only inspect its own row.** The mutual-cycle rule, the
   retired-component-in-production rule and the incident-timestamp rule all read
   other rows or tables, so they must be triggers.

2. **A column in a `CHECK` may not also carry a foreign-key referential
   action** (error 3823). `CHECK (component_id <> depends_on_id)` is a textbook
   single-row constraint, and MySQL still rejects it, because both columns are
   FKs with `CASCADE`. The cascade was worth keeping, so the rule moved into
   `trg_dependency_validate_insert` / `_update`.

**Demonstrate:** `mysql -u root -p --force --table infratrace < queries/09_dbms_features_demo.sql`
— the last section makes eight writes the database must refuse.

---

## 6. Indexing

Six explicit secondary indexes. Detailed rationale: [`indexing.md`](indexing.md)

| Index | Columns | Why this shape |
|---|---|---|
| `idx_dependency_reverse` | `(depends_on_id, component_id)` | **Covering** index for the reverse lookup that every level of blast-radius traversal performs. |
| `idx_deployment_comp_env_time` | `(component_id, environment_id, deployed_at DESC)` | Two equality filters then the sort — removes `Using filesort`. |
| `idx_incident_severity_status` | `(severity, status)` | More selective column leads. |
| `idx_incident_started_at` | `(started_at)` | Range predicate on the incident timeline. |
| `idx_component_owner_type` | `(owner_team_id, component_type)` | Ownership audits grouped by type. |
| `idx_deployment_time` | `(deployed_at)` | Global deployment timeline scans. |

**InnoDB auto-indexes every FK column**, so single-column indexes on
`dependency.component_id` and friends already exist and are not duplicated.
Verified: `deployment` has no standalone `fk_deployment_component` index because
`idx_deployment_comp_env_time` already leads with `component_id` and InnoDB
reuses it.

### B-tree behaviour, shown rather than asserted

An index only helps when the query gives it a **prefix to seek to**:

| Query | Plan | Why |
|---|---|---|
| `component_name LIKE 'Order%'` | `type=range` | known prefix, index seek |
| `component_name LIKE '%Service%'` | `type=index` | no prefix — scans all 2,000 entries |
| `YEAR(started_at) = 2026` | `type=index` | the function hides the column |
| `started_at >= '2026-01-01' AND < '2027-01-01'` | `type=range` | sargable rewrite of the same question |

---

## 7. Query Processing and Optimisation

Full analysis with measurements: [`query-optimization.md`](query-optimization.md)

At 19 components MySQL correctly ignores every index, so all measurements were
taken on a generated 2,000-component / 7,878-edge / 40,000-deployment dataset
(`database/performance/01_generate_load_data.sql`).

| Access pattern | Before | After | Gain |
|---|---|---|---|
| Reverse dependency lookup | 0.914 ms, `type=ALL` | 0.013 ms, covering index | ~70× |
| Latest deployment per component/env | 7.24 ms, `ALL` + filesort | 0.020 ms, `ref` | ~350× |
| Incident severity + status filter | 1.15 ms, `ALL` | 0.089 ms, `ref` | ~13× |
| Recursive blast radius | 8.0 ms, hash join per level | 3.5 ms, index lookup per level | ~2.3× |

### Two findings worth more than the speedups

**The optimiser's cost is not a prediction of time.** Adding
`incident_component(component_id, incident_id)` to the deploy-then-incident
correlation made the estimated cost **nine times better** and the measured query
**nearly three times slower** (39 ms → 108 ms): the new index made MySQL reorder
the join to drive from the 40,000-row `deployment` table instead of the
6,000-row `incident` table. **The index was rejected on measured evidence.**
This is why the project measures rather than adding indexes by reflex.

**The shape of a recursive CTE matters more than its index.** Carrying a
`depth` column in the recursion made the same traversal **15× slower**
(54.5 ms vs 3.7 ms) and materialised 31,331 rows instead of 1,942. The reason is
that the recursive `UNION` de-duplicates on the **whole row**, so
`(component 500, depth 3)` and `(component 500, depth 7)` are different rows and
the component is expanded once per reachable depth. `fn_blast_radius_count()`
therefore omits `depth` deliberately.

---

## 8. Transactions and ACID

Runnable demonstrations: [`database/transactions/`](../../database/transactions/) ·
Discussion: [`transactions.md`](transactions.md)

| Property | How InfraTrace demonstrates it |
|---|---|
| **Atomicity** | `01_acid_basics.sql` §3: record a deployment **and** close the related incident. The second statement violates `chk_incident_status`, the handler rolls back, and the *successful* first write is undone too. Verified: deployment count unchanged, incident still `Open`. |
| **Consistency** | 14 CHECK, 15 FK, 8 UNIQUE constraints and 5 triggers mean no transaction can commit a state that breaks a rule — including "no cycle in the dependency graph". |
| **Isolation** | `02_isolation_levels.sql` runs all four levels across two sessions. |
| **Durability** | InnoDB's redo log and `innodb_flush_log_at_trx_commit`. Once `COMMIT` returns, the change survives a crash. Not separately demonstrated — doing so honestly requires killing the server mid-transaction. |

`SAVEPOINT` and partial rollback are demonstrated in `01_acid_basics.sql` §5:
three dependency edges are inserted, and `ROLLBACK TO SAVEPOINT` discards only
the last one while the transaction stays open.

---

## 9. Concurrency Control

All of the following were reproduced with **two genuinely concurrent
connections**, not described hypothetically. Run them yourself with
`sh database/transactions/run_concurrency_demos.sh`.

| Phenomenon | Measured result |
|---|---|
| Non-repeatable read at `READ COMMITTED` | A reads `High`, B commits `Critical`, A re-reads → **`Critical`** |
| Repeatable read at `REPEATABLE READ` | A reads `High`, B commits `Critical`, A re-reads → **`High`**; after A commits → `Critical` |
| Dirty read at `READ UNCOMMITTED` | B reads `Low` from A's **uncommitted** transaction; A rolls back; the value never existed |
| Phantom rows at `REPEATABLE READ` | A counts 5, B inserts and commits, A re-counts → still **5**; after commit → 6 |

**Isolation levels and what each prevents**

| Level | Dirty read | Non-repeatable read | Phantom |
|---|---|---|---|
| READ UNCOMMITTED | possible | possible | possible |
| READ COMMITTED | prevented | possible | possible |
| REPEATABLE READ (MySQL default) | prevented | prevented | prevented\* |
| SERIALIZABLE | prevented | prevented | prevented |

\* InnoDB prevents phantoms at REPEATABLE READ via its MVCC snapshot for plain
reads and next-key (gap) locking for locking reads. The SQL standard only
requires this at SERIALIZABLE, so InnoDB is stricter than the standard.

### The lost update, and three cures

Two responders both read an empty `incident.root_cause`, both write their own
finding, and **neither transaction fails** — but one finding is destroyed.
Reproduced in `03_lost_update.sql` and by the automated runner.

Important: this happens **at REPEATABLE READ**. Repeatable read guarantees your
*reads* are consistent; it says nothing about whether your *write* is based on a
value that is still current.

| Cure | Mechanism |
|---|---|
| `SELECT … FOR UPDATE` | Exclusive lock at **read** time; the second session blocks and then reads the committed value. Verified: both findings survive. |
| Atomic read-modify-write | Compute inside the `UPDATE` (`SET root_cause = CONCAT(root_cause, …)`) so no window exists. |
| Optimistic concurrency | A version column plus `UPDATE … WHERE version = <the one I read>`, then check affected rows. |

### Locking behaviour, measured

| Behaviour | Result |
|---|---|
| Row-level, not table-level | A locks component 9001; B locks 9002 **immediately**; B on 9001 **blocks** |
| Readers never block writers | A holds an uncommitted `UPDATE`; B's plain `SELECT` returns the committed value instantly, and `fn_blast_radius_count()` still runs |
| Lock wait timeout | `ERROR 1205 … try restarting transaction` after `innodb_lock_wait_timeout` |
| `FOR UPDATE NOWAIT` | `ERROR 3572` immediately instead of waiting |
| `FOR UPDATE SKIP LOCKED` | returns only the unlocked row — the work-queue pattern |

---

## 10. Deadlocks

`05_deadlock.sql`, reproduced automatically.

**Cause.** Session A locks component 9001 then 9002; session B locks 9002 then
9001. The opposite ordering closes a cycle in the waits-for graph.

**Detection.** InnoDB finds the cycle immediately and kills one transaction:

```
ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction
```

Verified: `information_schema.innodb_metrics.lock_deadlocks` incremented 1 → 2.
(Note: `SHOW GLOBAL STATUS LIKE 'innodb_deadlocks'` does **not** exist in
MySQL 8 — that variable is MariaDB/Percona only.)

`SHOW ENGINE INNODB STATUS` then names both transactions, the exact statement
each was running, the locks held and requested, and which was rolled back.

**A pleasing symmetry:** InnoDB's deadlock detector and this project's
`sp_detect_dependency_cycles()` do the same job — find a cycle in a directed
graph. One walks *transaction waits for transaction*, the other walks
*component depends on component*.

**Prevention and handling**
1. Acquire locks in a consistent order (demo 2 proves the deadlock disappears).
2. Keep transactions short — never hold locks across user think time.
3. Drive `UPDATE`/`DELETE` by an index, so fewer rows are locked.
4. Always retry: `sp_demo_retry_on_deadlock` catches `SQLSTATE '40001'`, rolls
   back, backs off and retries. Deadlocks cannot be eliminated entirely, so
   handling them is mandatory.

---

## 11. Views

`database/views/views.sql` — four views, each solving a real repetition.

| View | Purpose | Concept shown |
|---|---|---|
| `component_ownership_view` | every component with its owner; unowned shown as `UNASSIGNED` | `LEFT JOIN` + `COALESCE` — an inner join would hide exactly the rows an audit wants |
| `production_infrastructure_view` | the latest **successful** production deployment per component | correlated subquery on `MAX(deployed_at)` |
| `incident_impact_view` | one row per incident/component pair with the responsible team | four-way join reused by several queries |
| `dependency_edge_view` | the graph with both endpoints resolved to names | **self-join** hidden behind a readable interface |

Views provide **logical data independence**: queries and the API read
`production_infrastructure_view`, so when the rollback semantics changed
(`status` split into `status` + `is_rollback`), the view absorbed the change and
no consumer needed editing.

They also gave the project a **regression test**: the view once reported
Inventory Service running `v4.0.0` — the version that had been rolled back.
`tests/sql/04_stored_programs.sql` now asserts it reports `v3.9.0`.

---

## 12. Stored Procedures

`database/procedures/procedures.sql` — five.

| Procedure | Parameters | Returns |
|---|---|---|
| `sp_get_dependencies` | `component_id` | full transitive chain of what it needs, with depth |
| `sp_get_blast_radius` | `component_id` | everything affected if it fails, with hops and team |
| `sp_team_health_report` | `team_id` | per-component risk metrics for one team |
| `sp_component_impact_report` | `component_id` | **five result sets**: component, dependencies, blast radius, applications, incident history |
| `sp_detect_dependency_cycles` | none | any cycle in the graph; no rows = acyclic |

Why procedures rather than application code: the logic lives next to the data,
executes in one round trip, and is available identically to the mysql client,
MySQL Workbench and the API — so there is exactly one definition of
"blast radius".

`sp_component_impact_report` also produced a genuine engineering lesson: it
originally *called* `sp_get_blast_radius`, and the nested `CALL` did not
reliably surface its result set to the client driver, so callers silently read
the next section's rows. The traversal is now inlined.

---

## 13. Stored Functions

`database/functions/functions.sql` — six.

| Function | Returns | Notes |
|---|---|---|
| `fn_direct_dependent_count(id)` | INT | direct dependents |
| `fn_blast_radius_count(id)` | INT | **recursive CTE inside a function** |
| `fn_dependency_depth(id)` | INT | longest chain downwards |
| `fn_incident_count(id)` | INT | incident involvements |
| `fn_would_create_cycle(from, to)` | TINYINT | reachability test enabling cycle **prevention** |
| `fn_risk_score(id)` | INT | blast radius × criticality weight |

All are declared `NOT DETERMINISTIC` + `READS SQL DATA`, which is the honest
declaration: they read tables, so the same argument can give a different answer
after the data changes. They were originally mis-declared `DETERMINISTIC`, which
is unsafe with statement-based binary logging; `tests/sql/01_schema.sql` now
asserts no function is marked deterministic.

`fn_risk_score` exists so the ranking formula is defined **once** and cannot
drift between the SQL reports, the procedures and the dashboard.

---

## 14. Triggers

`database/triggers/triggers.sql` — five, each on both `INSERT` and `UPDATE`
where a write path could otherwise bypass the rule.

| Trigger | Event | Rule |
|---|---|---|
| `trg_dependency_validate_insert` | BEFORE INSERT | no self-dependency; **no cycle of any length up to the recursion limit** |
| `trg_dependency_validate_update` | BEFORE UPDATE | same rules on edit |
| `trg_deployment_validate_insert` | BEFORE INSERT | a retired component cannot reach production |
| `trg_deployment_validate_update` | BEFORE UPDATE | the same rule, so `UPDATE` cannot bypass it |
| `trg_incident_set_resolved_at` | BEFORE UPDATE | closing an incident stamps `resolved_at`; reopening clears it |

The UPDATE variants exist because an audit found the original INSERT-only
trigger could be bypassed: `UPDATE deployment SET component_id = 10,
environment_id = 3` moved a retired component into production unchecked.

---

## 15. Recursive Queries

`database/queries/07_blast_radius.sql` — seven recursive queries.

A recursive CTE has an **anchor member** (the seed) and a **recursive member**
that references the CTE, combined with `UNION` or `UNION ALL`:

```sql
WITH RECURSIVE impact (component_id, depth) AS (
    SELECT component_id, 0                    -- anchor: the failed component
    FROM component WHERE component_name = 'Payment DB'

    UNION                                     -- de-duplicates; guarantees termination

    SELECT d.component_id, im.depth + 1       -- recursive: whoever depends on the set
    FROM dependency d
    JOIN impact im ON d.depends_on_id = im.component_id
    WHERE im.depth < 20                       -- safety stop
)
SELECT ...
```

Produces, from the real dataset:

```
Payment DB  ←  Payment Service  ←  Order Service  ←  API Gateway
   hop 0          hop 1              hop 2            hop 3
```

**`UNION` vs `UNION ALL`.** `UNION` de-duplicates, which both prevents infinite
recursion on cyclic data and avoids the path explosion described in section 7.
`UNION ALL` is used only in R2, where each distinct *path* is the information
being reported.

**Termination is guaranteed three ways:** the triggers make the graph acyclic;
`UNION` de-duplicates; and an explicit depth cap (1000, matching MySQL's default
`cte_max_recursion_depth`) stops runaway recursion even if the first two were
somehow bypassed. The `UNION ALL` walks that carry a path string cannot rely on
de-duplication, so those are bounded by the component count instead - the exact
bound for a simple cycle. Verified: with a cycle deliberately
inserted (triggers dropped), `fn_blast_radius_count` still returned in
milliseconds rather than hanging.

**Other recursive uses:** `fn_would_create_cycle` (reachability, enabling
prevention), `sp_detect_dependency_cycles` (detection, carrying the path
string), and R5, which runs the traversal from all 19 origins simultaneously to
rank the whole platform.

---

## 16. Where each concept is demonstrated

| Concept | File | Command |
|---|---|---|
| Schema, keys, constraints | `database/schema/schema.sql` | `SHOW CREATE TABLE component;` |
| Normalisation | `docs/database/normalization.md` | — |
| Constraints firing | `database/queries/09_dbms_features_demo.sql` | `mysql --force --table infratrace < queries/09_dbms_features_demo.sql` |
| Indexes | `database/performance/02_explain_analysis.sql` | `mysql --table < performance/02_explain_analysis.sql` |
| Query optimisation | same | `EXPLAIN ANALYZE` output inline |
| Transactions, ACID | `database/transactions/01_acid_basics.sql` | `mysql --table infratrace < transactions/01_acid_basics.sql` |
| Isolation levels | `database/transactions/02_isolation_levels.sql` | two sessions, or the automated runner |
| Lost update | `database/transactions/03_lost_update.sql` | ″ |
| Locking | `database/transactions/04_row_locking.sql` | ″ |
| Deadlock | `database/transactions/05_deadlock.sql` | ″ |
| All concurrency, automated | `run_concurrency_demos.sh` | `sh transactions/run_concurrency_demos.sh` |
| Views | `database/views/views.sql` | `SELECT * FROM production_infrastructure_view;` |
| Procedures / functions | `database/procedures/`, `functions/` | `CALL sp_get_blast_radius(11);` |
| Triggers | `database/triggers/triggers.sql` | demo file, part 5 |
| Recursive queries | `database/queries/07_blast_radius.sql` | `mysql --table infratrace < queries/07_blast_radius.sql` |
| Verification | `tests/run_all_tests.sql` | `cd tests && mysql --table < run_all_tests.sql` |
