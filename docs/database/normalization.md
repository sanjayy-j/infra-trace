# InfraTrace — Normalisation

The schema is in **BCNF**. This document shows the working rather than
asserting the result: functional dependencies first, then each normal form
tested against them, then the anomalies the design prevents and the two places
where a deliberate decision was made not to normalise further.

---

## 1. Functional dependencies, table by table

Notation: `X → Y` means X functionally determines Y.

### `team`
```
team_id    → team_name, team_email, created_on
team_name  → team_id, team_email, created_on      (candidate key, UNIQUE)
team_email → team_id, team_name, created_on       (candidate key, UNIQUE)
```
Three candidate keys. `team_id` is the chosen primary key.

### `developer`
```
developer_id → developer_name, email, job_role, team_id, joined_on
email        → developer_id, ...                  (candidate key, UNIQUE)
```
`team_id → team_name` is *not* an FD of this relation, because `team_name` is
not an attribute of it — which is the whole point of storing only the foreign
key.

### `application`
```
application_id   → application_name, app_type, owner_team_id, description
application_name → application_id, ...            (candidate key, UNIQUE)
```

### `component`
```
component_id   → component_name, component_type, owner_team_id,
                 criticality, tech_stack, is_active, created_on
component_name → component_id, ...                (candidate key, UNIQUE)
```

The FD worth checking explicitly is one that does **not** hold:

```
owner_team_id  →  criticality        ✗  NOT an FD
component_type →  criticality        ✗  NOT an FD
```

Two components owned by Payments Team have different criticality (`Payment DB`
is Critical, `Stripe Payment API` is High). Two Databases likewise differ
(`Payment DB` Critical, `Inventory DB` High). So there is no transitive
dependency `component_id → owner_team_id → criticality` to eliminate.

### `application_component`
```
(application_id, component_id) → usage_notes
```
Neither half alone determines `usage_notes`: ShopSphere Web's note about API
Gateway ("All browser traffic enters through the gateway") differs from
ShopSphere Mobile's note about the same component ("Mobile API traffic"). The
attribute genuinely depends on the pair.

### `dependency`
```
dependency_id                 → component_id, depends_on_id,
                                dependency_type, is_critical, description
(component_id, depends_on_id) → dependency_id, dependency_type,
                                is_critical, description     (UNIQUE)
```
Two candidate keys: the surrogate, and the natural pair.

### `environment`
```
environment_id   → environment_name, region, is_production
environment_name → environment_id, ...            (candidate key, UNIQUE)
```

Worth noting: `environment_name → is_production` happens to hold in the current
data (only the row named 'Production' is flagged), but that is a property of
today's rows, not a constraint. A second production region named `Production-EU`
would break it. `is_production` is modelled as an attribute of the environment
*entity*, determined by `environment_id`, which is why queries filter on the
flag rather than on the string.

### `deployment`
```
deployment_id                               → component_id, environment_id, version,
                                              deployed_by, deployed_at, status, is_rollback
(component_id, environment_id, deployed_at) → deployment_id, version, status,
                                              deployed_by, is_rollback   (UNIQUE)
```
The natural key says a component cannot be deployed into the same environment
at the same instant. It is enforced by `uq_deployment_event`, added after an
audit found that an exact duplicate deployment row was being accepted.

A non-FD worth stating:
```
(component_id, environment_id) → version     ✗  NOT an FD
```
A component has many versions in one environment over time. That is why
"what is running now" needs `MAX(deployed_at)` and lives in a view.

### `incident`
```
incident_id → title, severity, status, environment_id, reported_by,
              started_at, resolved_at, root_cause,
              caused_by_deployment_id, updated_at
```
Single candidate key. And a non-FD that matters:
```
status → resolved_at        ✗  NOT an FD
```
`status = 'Resolved'` requires *a* timestamp — enforced by
`chk_incident_resolved` — but does not determine *which* one. A constraint and a
functional dependency are different things, and conflating them would wrongly
suggest a normalisation problem here.

### `incident_component`
```
(incident_id, component_id) → impact_level
```
Again both halves are needed: Payment DB is `RootCause` in incident 1 and
`Degraded` in incident 18.

---

## 2. First Normal Form

**Rule.** Every attribute is atomic; no repeating groups; no lists in a column.

**Satisfied.** Every column holds a single value.

The design decision this forced is the most important one in the project. The
obvious shortcut would be:

```sql
-- REJECTED
CREATE TABLE component (
    component_id   INT PRIMARY KEY,
    component_name VARCHAR(100),
    dependencies   TEXT       -- 'Payment DB, Redis Cache, Kafka Event Bus'
);
```

That violates 1NF, and every capability of this project dies with it:

| Lost | Why |
|---|---|
| Referential integrity | no FK can point into the middle of a string |
| Joins | cannot join a comma-separated list to `component` |
| Indexing | `LIKE '%Redis%'` cannot use an index |
| Edge attributes | nowhere to record `dependency_type` or `is_critical` |
| **Recursive traversal** | `WITH RECURSIVE` needs rows to join to |

Blast-radius analysis exists **because** dependencies are rows. The same
reasoning rejects a component list on `application` and an affected-component
list on `incident`; both became bridge tables.

---

## 3. Second Normal Form

**Rule.** 1NF, and no non-key attribute depends on only *part* of a composite
key.

Only three relations have composite candidate keys, so only three need testing.

| Relation | Composite key | Non-key attributes | Verdict |
|---|---|---|---|
| `application_component` | `(application_id, component_id)` | `usage_notes` | depends on both halves — **2NF** |
| `incident_component` | `(incident_id, component_id)` | `impact_level` | depends on both halves — **2NF** |
| `deployment` | `(component_id, environment_id, deployed_at)` | `version`, `status`, `deployed_by`, `is_rollback` | all depend on the full event — **2NF** |

Every other relation has a single-attribute primary key, so partial dependency
is impossible and 2NF holds trivially.

A concrete partial dependency that was avoided: putting `component_type` into
`application_component`. It would depend on `component_id` alone — half the key
— and would then have to be kept in step with `component.component_type`.

---

## 4. Third Normal Form

**Rule.** 2NF, and no non-key attribute transitively depends on the key through
another non-key attribute.

The violations deliberately avoided:

| Denormalisation avoided | The transitive FD it would create | Anomaly it would cause |
|---|---|---|
| `component.team_name` | `component_id → owner_team_id → team_name` | renaming a team means updating many component rows |
| `deployment.component_name` | `deployment_id → component_id → component_name` | renaming a component means rewriting deployment history |
| `deployment.environment_name` | `deployment_id → environment_id → environment_name` | same |
| `incident.reporter_email` | `incident_id → reported_by → email` | an engineer changing email address rewrites incident history |
| `incident_component.component_type` | `… → component_id → component_type` | reclassifying a component means updating incident rows |

Human-readable names are reassembled at query time by joining, or by the views
in `database/views/views.sql`. The views exist **precisely** so that nobody is
tempted to denormalise for convenience: `dependency_edge_view` gives the graph
with both endpoints named, and `component_ownership_view` gives every component
with its team name, without either being stored redundantly.

---

## 5. Boyce-Codd Normal Form

**Rule.** For every non-trivial FD `X → Y`, X is a superkey.

Every determinant listed in section 1 is a candidate key, so the schema is in
**BCNF**, which is strictly stronger than the 3NF usually asked for.

The relation worth examining is `dependency`, which has two determinants:

```
dependency_id                 → everything     (primary key)
(component_id, depends_on_id) → everything     (UNIQUE)
```

Both are candidate keys, so no determinant is a non-key attribute and BCNF
holds.

A relation in 3NF but not BCNF requires **overlapping composite candidate keys**
with a non-trivial FD from a proper subset of one of them. No relation here has
that shape: the three composite keys have no non-key attribute determining part
of the key.

---

## 6. Fourth Normal Form (informal)

No relation has two independent multi-valued dependencies on the same key.

The case to check is `component`, which is independently related to *many*
dependencies and *many* deployments. Because those live in separate relations
(`dependency`, `deployment`) rather than being combined into one table, no
multi-valued dependency arises. Combining them — a table of
`(component_id, depends_on_id, deployment_id)` — would produce a cross product
of unrelated facts, which is exactly the 4NF violation.

---

## 7. Anomalies prevented, with concrete examples

| Anomaly | Concrete example if denormalised | Prevented by |
|---|---|---|
| **Update** | "Payments Team" is renamed. Denormalised: 3 component rows + 2 application rows + incident rows must all change, and any missed row corrupts ownership reports. | `owner_team_id` FK — one row changes |
| **Insert** | A new team is formed but owns nothing yet. Denormalised: it cannot be recorded at all. | `team` is its own relation |
| **Delete** | The last component of "Data & Insights" is retired. Denormalised: the team disappears. | `team` is its own relation; `ON DELETE SET NULL` on `component.owner_team_id` |
| **Inconsistency** | The same edge `Order Service → Payment Service` recorded twice with different `is_critical` values, so blast radius depends on which row is read. | `uq_dependency_edge UNIQUE (component_id, depends_on_id)` |
| **Duplicate event** | The same deployment ingested twice by a retrying script, doubling the deployment count in every report. | `uq_deployment_event UNIQUE (component_id, environment_id, deployed_at)` |

---

## 8. Where normalisation was deliberately NOT taken further

Normalisation is a tool, not a score. Two places where a further step was
considered and rejected, with the reasoning recorded so the decision can be
challenged.

### 8.1 Enumerations are `VARCHAR` + `CHECK`, not lookup tables

Strictly, `component_type`, `criticality`, `severity`, `impact_level`,
`dependency_type` and `status` could each become a lookup table with an FK. That
is more "normalised" in the sense of removing repeated strings.

**Rejected**, because:

- The values are a **fixed, small, stable vocabulary**, not data the users
  manage. They change when the *schema* changes, not when the data does.
- Six extra tables and six extra joins on every analytical query, for no
  integrity gain — `CHECK` already rejects an invalid value just as an FK would.
- Queries stay readable: `WHERE criticality = 'Critical'` rather than
  `JOIN criticality_lookup cl ON … WHERE cl.name = 'Critical'`.
- Repeating the string is **not** a redundancy anomaly. There is no risk of two
  rows disagreeing about what `'Critical'` means, because the string *is* the
  value, not a copy of a fact stored elsewhere.

The trade-off, stated honestly: adding a new value requires `ALTER TABLE` rather
than an `INSERT`. For a vocabulary that changes roughly never, that is the
cheaper side of the trade.

`ENUM` was also rejected — it is not portable to other SQL dialects and its
storage is order-dependent, which makes reordering values hazardous.

### 8.2 `updated_at` on `incident`

`incident.updated_at` is maintained automatically by
`ON UPDATE CURRENT_TIMESTAMP`. It is metadata about the row rather than a fact
about the incident, and it is functionally dependent on `incident_id` like any
other attribute, so it does not affect the normal form.

---

## 9. Summary

| Normal form | Status | Basis |
|---|---|---|
| 1NF | Satisfied | all attributes atomic; dependency/application/incident lists are bridge tables |
| 2NF | Satisfied | the three composite-key relations each have attributes depending on the whole key |
| 3NF | Satisfied | no name or email duplicated alongside the foreign key that determines it |
| BCNF | Satisfied | every determinant is a candidate key |
| 4NF | Satisfied (informally) | independent multi-valued facts live in separate relations |

Verified by `tests/sql/03_integrity.sql`, which asserts the invariants that
normalisation is supposed to guarantee — no duplicate edges, no orphan rows, no
contradictory incident state — and by the constraint tests in
`tests/sql/02_constraints.sql`, which prove the database actively rejects the
writes that would break them.
