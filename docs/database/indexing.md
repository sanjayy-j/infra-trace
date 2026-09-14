# InfraTrace — Indexing Strategy

Six explicit secondary indexes, **each with a measurement behind it**, and one
that was **rejected on measured evidence**.

Measurements and `EXPLAIN` output: [`query-optimization.md`](query-optimization.md)

---

## 1. What InnoDB gives you for free

Two things are already indexed before any `CREATE INDEX` is written, and
duplicating them would waste space and slow every write.

**The primary key is the table.** InnoDB stores rows in a clustered B-tree keyed
by the primary key, so a lookup by `component_id` is already optimal. Secondary
indexes store the primary key as their row pointer, which is why a narrow
primary key is worth having.

**Every foreign key column is indexed automatically.** InnoDB requires an index
on the referencing column to check referential integrity, and creates one if you
do not. So `dependency.component_id`, `deployment.component_id`,
`incident_component.component_id` and the rest already have indexes.

**Verified on the live schema** — `deployment` has no standalone
`fk_deployment_component` index, because `idx_deployment_comp_env_time` already
leads with `component_id` and InnoDB reused it rather than creating a duplicate:

```
deployment | fk_deployment_developer       | 1
deployment | fk_deployment_environment     | 1
deployment | idx_deployment_comp_env_time  | 3    <- also serves the FK
deployment | idx_deployment_time           | 1
deployment | PRIMARY                       | 1
```

The same applies to `dependency`, where `uq_dependency_edge (component_id,
depends_on_id)` serves one FK and `idx_dependency_reverse (depends_on_id,
component_id)` serves the other.

**This is why the project does not create single-column indexes on FK columns.**
A naive checklist would add six redundant indexes here.

---

## 2. The six indexes

### `idx_dependency_reverse (depends_on_id, component_id)`

**Query:** "what depends on component X?" — and therefore every level of every
blast-radius traversal.

```sql
SELECT component_id FROM dependency WHERE depends_on_id = ?;
```

**Column order:** `depends_on_id` must lead because it is the equality filter.
`component_id` second makes the index **covering** — the answer is read entirely
from the index with no lookup into the table.

**Measured:** ~0.9 ms → ~0.013 ms (roughly 70x). The plan change is the
durable part: `ALL` → `ref` + `Using index`.

This is the single most valuable index in the project, because its cost is
multiplied by the depth of the graph on every traversal.

---

### `idx_deployment_comp_env_time (component_id, environment_id, deployed_at DESC)`

**Query:** "what version of component X is running in environment E?" —
resolved once per component to build `production_infrastructure_view`.

```sql
SELECT version FROM deployment
WHERE component_id = ? AND environment_id = ? AND status = 'Success'
ORDER BY deployed_at DESC LIMIT 1;
```

**Column order** follows the query shape exactly:
1. `component_id` — equality
2. `environment_id` — equality
3. `deployed_at DESC` — the sort

Putting the sort column last lets the index **supply the ordering**, so MySQL
stops at the first row instead of sorting 40,000. `DESC` is explicit because
MySQL 8 supports genuinely descending indexes.

**Measured:** ~7 ms → ~0.02 ms (two to three orders of magnitude), and
`Using filesort` disappears.

`status` is deliberately **not** in the index: it is filtered after the lookup,
and adding a low-cardinality column ahead of the sort column would break the
ordering benefit.

---

### `idx_incident_severity_status (severity, status)`

**Query:** the dashboard's "open SEV1 incidents" headline.

**Column order:** `severity` leads because it is more selective here — SEV1 is
~8% of rows versus `Open` at ~20% — and the more selective column first discards
more rows earlier in the descent.

**Measured:** ~1.2 ms → ~0.09 ms (roughly an order of magnitude).

---

### `idx_incident_started_at (started_at)`

**Query:** the range predicate in deploy-then-incident correlation, and every
"recent incidents" filter.

**Measured:** ~39 ms → ~34 ms on the correlation query. A modest gain and
within run-to-run variance on its own, but it is cheap, it is consistent across
repeat runs, and it also serves the monthly trend query.

---

### `idx_component_owner_type (owner_team_id, component_type)`

**Query:** ownership audits — "all components owned by team X, grouped by type".

**Column order:** `owner_team_id` leads because it is the equality filter.
`component_type` second does double duty — it satisfies the `GROUP BY` from
index order (removing the temporary table) and makes the index **covering**.

**Measured:** ~0.96 ms → ~0.04 ms; plan goes `ALL` + `Using temporary` →
`ref` + `Using index`.

Composite rather than two single-column indexes because the leading column
alone also serves the unowned-components audit: `WHERE owner_team_id IS NULL`
becomes a covering lookup (~0.02 ms). Note it covers `owner_team_id` alone
(leftmost prefix) but **not** `component_type` alone.

---

### `idx_deployment_time (deployed_at)`

**Query:** global deployment-timeline scans not scoped to one component — "the
most recent deployment anywhere", which anchors the "recent" window in the
analytical queries, and month-range counts.

This is a genuinely different access path from
`idx_deployment_comp_env_time`. That index leads with `component_id`, so with no
component filter it can only be *scanned*, not sought.

**Measured:**

| Query | Before | After |
|---|---|---|
| `MAX(deployed_at)` | covering scan of ~40,000 entries, ~4.7 ms | **`Select tables optimized away`** — no rows examined |
| one-month range count | ~4.4 ms | ~0.3 ms |

The `MAX()` case is the neatest result in the study: the maximum of an indexed
column is the last entry of its B-tree, so the optimiser answers it during
planning without examining a single row.

---

## 3. The index that was rejected

`incident_component (component_id, incident_id)`.

The reasoning for adding it is textbook: the table's primary key is
`(incident_id, component_id)`, and the deploy-then-incident correlation query
traverses it from the **component** side, which that key cannot serve.

**Measured, all four combinations:**

| Configuration | Optimiser cost | Measured |
|---|---|---|
| no extra index | 46,065 | 39.2 ms |
| `+ incident(started_at)` | 46,065 | **34.4 ms** |
| `+ incident_component(component_id, incident_id)` | 5,001 | **108 ms** |
| both | 5,001 | **111 ms** |

The index makes the optimiser's cost estimate **nine times better** and, on
Linux, the query **roughly three times slower**, because the optimiser then
reorders the join to drive from the 40,000-row `deployment` table instead of the
6,000-row `incident` table.

**Rejected** - but the reasoning deserves care, because the evidence is not
equally strong on every platform:

- The **plan** change is reproducible everywhere. On both Linux and Windows the
  bridge index makes MySQL full-scan all 39,233 `deployment` rows and then
  perform ~112,000 index lookups, instead of driving from the much smaller
  `incident` table. That is objectively more work.
- The **timing** penalty is only measurable on Linux. Re-measured natively on
  Windows, all three configurations land in 521-753 ms with overlapping ranges,
  because the query is I/O-bound there and the noise exceeds the difference.

So the rejection rests on the plan, corroborated by the Linux timings. Quoting
only the Linux figures would have overstated the case. See
[`query-optimization.md` section 9b](query-optimization.md) for the full
cross-platform table.

---

## 4. When an index cannot help

Two rules, demonstrated on the same column so the comparison is exact.

| Query | Plan | Why |
|---|---|---|
| `component_name LIKE 'Order%'` | `range` | known prefix to seek to |
| `component_name LIKE '%Service%'` | `index` | no prefix — scans all entries |
| `YEAR(started_at) = 2026` | `index` | the function hides the column |
| `started_at >= '2026-01-01' AND < '2027-01-01'` | `range` | sargable rewrite |

A B-tree is ordered by prefix. A leading wildcard gives it nothing to seek to.
Wrapping an indexed column in a function makes the predicate **non-sargable**,
because the index stores `started_at`, not `YEAR(started_at)`.

Both of these show as `type=index`, not `type=ALL` — MySQL scans the *index*,
which is narrower than the table, but it is emphatically not a lookup.

---

## 5. The cost side

| Cost | Detail |
|---|---|
| Disk | 40–65% of table data size on the larger tables |
| Write amplification | every `INSERT`/`UPDATE`/`DELETE` maintains every affected index |
| Optimiser risk | more candidate plans, and occasionally a worse choice — as Pattern 5 showed |

For InfraTrace the trade is clearly correct: writes are rare (an occasional
deployment or incident) and reads are constant. An OLTP system with a heavy
write path would weigh it differently.

**A concurrency benefit worth noting:** an index-driven `UPDATE` or `DELETE`
locks only the rows it matches, while a full scan locks far more. So these
indexes also reduce lock contention and deadlock probability — see
[`transactions.md`](transactions.md).

---

## 6. Method

1. Start from the **queries the project actually runs**, not from a checklist.
2. Check what InnoDB already provides (PK and FK indexes).
3. Choose column order from the query shape: equality filters first, most
   selective first, sort column last.
4. Prefer a **covering** index where the query selects few columns.
5. **Measure with `EXPLAIN ANALYZE` at realistic data volume.**
6. Keep the index only if the measurement supports it — and record the rejects,
   because a documented rejection is as useful as an accepted index.

Every index in the final set now has a measurement behind it. Two
(`idx_component_owner_type`, `idx_deployment_time`) were originally kept on
reasoning alone; an audit flagged that as inconsistent with this very method, so
they were measured (patterns 6 and 7) rather than left asserted. Both turned out
to be justified — but that was a finding, not a foregone conclusion, and one
index elsewhere in the study failed the same test and was dropped.
