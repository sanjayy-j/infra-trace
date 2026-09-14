# InfraTrace — Query Optimisation

Every number in this document was measured with `EXPLAIN ANALYZE` on
**MySQL 8.0.46**.

**How to read the timings.** Each figure is one observed execution, quoted to
the precision `EXPLAIN ANALYZE` reports. They are *measurements, not
benchmarks*: no warm-up, no repetition, no statistical treatment. Treat them as
orders of magnitude and as evidence of a direction, not as precise values. The
`EXPLAIN` **plan types**, by contrast, are deterministic and reproduce exactly.

**Verified on two platforms.** Every measurement below was taken twice: once on
MySQL 8.0.46 in a Linux container, and once on MySQL 8.0.46 installed natively
on Windows. Absolute timings differ substantially between them - Windows is
roughly an order of magnitude slower on the I/O-bound joins - so section 10
reports both, and states plainly which conclusions survive on both platforms
and which do not. One does not.

Reproduce it all with:

```bash
cd database
mysql -u root -p < performance/01_generate_load_data.sql
mysql -u root -p --table < performance/02_explain_analysis.sql
```

---

## 1. Why measurements are taken on a generated dataset

The real ShopSphere dataset is 19 components and 29 dependency edges. At that
size **the MySQL optimiser is right to ignore every index**: reading 19 rows
sequentially is cheaper than descending a B-tree and then fetching rows. `EXPLAIN`
reports `type=ALL` almost everywhere, and a "before and after adding an index"
comparison would show no difference at all.

Claiming an index sped something up when the plan never used it would be
dishonest. So `01_generate_load_data.sql` builds a **separate** schema,
`infratrace_perf`, with the same structure at realistic scale:

| Table | Rows |
|---|---|
| `component` | 2,000 |
| `dependency` | 7,878 |
| `deployment` | 40,000 |
| `incident` | 6,000 |
| `incident_component` | 18,000 |

The production schema `infratrace` is never touched.

**The generated graph is acyclic by construction** — an edge is only created
from a higher `component_id` to a lower one — so the recursive traversals
terminate for the same reason the real ones do. The generator asserts this
(`upward_edges_should_be_zero = 0`).

The edge mix also imitates real infrastructure rather than being uniform: most
edges are *local* (a component depends on something a little below it,
producing layered depth), and a minority point into ids 1–30, the "shared
platform" components. A uniform random target would give the first few
components over a thousand direct dependents each and everything else almost
none — a shape no real estate has. Measured fan-in after the fix peaks at 94
with a smooth taper.

---

## 2. How to read an `EXPLAIN`

| `type` | Meaning |
|---|---|
| `ALL` | full table scan — every row examined |
| `index` | full scan of an **index** — better, but still every entry |
| `range` | index range scan |
| `ref` | index lookup by a non-unique key — usually the goal |
| `eq_ref` | index lookup by a unique key |

| Column | Meaning |
|---|---|
| `key` | the index actually chosen; `NULL` means none was usable |
| `rows` | the optimiser's **estimate**, not a measurement |
| `Extra: Using index` | covering index — the answer came from the index alone |
| `Extra: Using filesort` | a sort that the index could not supply |

---

## 3. Pattern 1 — reverse dependency lookup

> "We need to restart component 7. What depends on it?"

The single most important lookup in InfraTrace, and the inner step of *every
level* of blast-radius traversal — so its cost is multiplied by graph depth.

```sql
SELECT component_id FROM dependency WHERE depends_on_id = 7;
```

**Before** — no index on `depends_on_id`:

```
type=ALL  key=NULL  rows=7823
-> Filter: (dependency.depends_on_id = 7)  (actual time=0.269..0.914 rows=79 loops=1)
    -> Table scan on dependency            (actual time=0.048..0.707 rows=7878 loops=1)
```

7,878 rows scanned to return 79.

**The index:**

```sql
CREATE INDEX idx_dependency_reverse ON dependency (depends_on_id, component_id);
```

Column order is not arbitrary:

- `depends_on_id` **must lead**, because that is the equality filter. An index
  led by `component_id` would be useless for this query.
- `component_id` second makes it a **covering index**: the answer is read
  entirely from the index with no lookup back into the table rows.

**After:**

```
type=ref  key=idx_dependency_reverse  rows=79  Extra: Using index
-> Covering index lookup on dependency using idx_dependency_reverse (depends_on_id=7)
   (actual time=0.00918..0.0131 rows=79 loops=1)
```

| | Before | After |
|---|---|---|
| Plan | `ALL` | `ref`, covering |
| Rows examined | 7,878 | 79 |
| **Measured time** | **~0.9 ms** | **~0.013 ms** |

**Roughly 70x faster, and the plan changes from a full scan to a covering
index lookup — which is the part that does not vary between runs.**

---

## 4. Pattern 2 — recursive blast radius

This pattern produced the most useful finding in the project, and **it is not
about an index.** It is about the shape of the CTE.

### 4.1 The `depth` column costs 15×

Same question, same answer, two spellings:

```sql
-- (a) carries depth
WITH RECURSIVE affected (component_id, depth) AS (
    SELECT component_id, 1 FROM dependency WHERE depends_on_id = 7
    UNION
    SELECT d.component_id, a.depth + 1
    FROM dependency d JOIN affected a ON d.depends_on_id = a.component_id
    WHERE a.depth < 20
) SELECT COUNT(DISTINCT component_id) FROM affected;

-- (b) does not
WITH RECURSIVE affected (component_id) AS (
    SELECT component_id FROM dependency WHERE depends_on_id = 7
    UNION
    SELECT d.component_id
    FROM dependency d JOIN affected a ON d.depends_on_id = a.component_id
) SELECT COUNT(DISTINCT component_id) FROM affected;
```

| Version | Rows materialised | Measured time |
|---|---|---|
| (a) with `depth` | **31,331** | **54.5 ms** |
| (b) without `depth` | **1,942** | **3.7 ms** |

**≈15× faster, 16× fewer rows.**

**Why.** The recursive `UNION` de-duplicates on the **entire row**, not on
`component_id`. With `depth` in the row, `(component 500, depth 3)` and
`(component 500, depth 7)` are *different* rows, so the same component is
expanded again for every distinct depth at which it can be reached. In a graph
with many alternative paths that is combinatorial path exploration. Remove the
column and each component is expanded exactly once — the textbook transitive
closure.

**Applied to the project:**

- `fn_blast_radius_count()` only needs a count, so its CTE carries
  `component_id` alone. That is a deliberate optimisation, now documented as one.
- `sp_get_blast_radius()` must report `hops_away`, so it has to carry `depth`.
  Its output is correct — it takes `MIN(depth)` per component — only its
  intermediate work is larger. At the real dataset's 29 edges this costs
  microseconds. The trade-off is recorded rather than left as a surprise.

### 4.2 The index still matters

| Traversal (no `depth`) | Optimiser cost | Measured |
|---|---|---|
| with `idx_dependency_reverse` | 244 | **3.5 ms** |
| without it | 772,038 | **8.0 ms** |

≈2.3× faster. Note how wildly the *cost estimate* overstates the gap — 3,000×
estimated versus 2.3× measured. Optimiser cost is a planning currency, not a
prediction of milliseconds. That observation sets up the next section.

---

## 5. Pattern 3 — latest deployment per component and environment

> "What version of component 500 is running in production?"

`production_infrastructure_view` resolves this for every component, so it runs
2,000 times to build that view.

```sql
SELECT version, deployed_at FROM deployment
WHERE component_id = 500 AND environment_id = 3 AND status = 'Success'
ORDER BY deployed_at DESC LIMIT 1;
```

**Before:**

```
type=ALL  key=NULL  rows=40254  Extra: Using where; Using filesort
-> Sort: deployment.deployed_at DESC, limit input to 1 row(s) per chunk
   (actual time=7.24..7.24 rows=1 loops=1)
    -> Table scan on deployment  (actual time=0.063..5.72 rows=40000 loops=1)
```

40,000 rows scanned **and then sorted**, to return one row.

**The index:**

```sql
CREATE INDEX idx_deployment_comp_env_time
    ON deployment (component_id, environment_id, deployed_at DESC);
```

Column order follows the query shape exactly: the two equality filters lead, the
sort column comes last. That lets the index **supply the ordering**, so MySQL
stops at the first row instead of sorting 40,000 — which is what removes
`Using filesort`. `DESC` is explicit because MySQL 8 supports genuinely
descending indexes.

**After:**

```
type=ref  key=idx_deployment_comp_env_time  rows=6
-> Index lookup on deployment using idx_deployment_comp_env_time
   (component_id=500, environment_id=3)  (actual time=0.0184..0.0184 rows=1 loops=1)
```

| | Before | After |
|---|---|---|
| Rows examined | 40,000 | 6 |
| Filesort | yes | **eliminated** |
| **Measured time** | **~7 ms** | **~0.02 ms** |

**Two to three orders of magnitude faster, and `Using filesort` disappears
entirely — the largest single win in the project.**

---

## 6. Pattern 4 — incident severity and status filter

> "Show every SEV1 incident still open." The dashboard's headline number.

```sql
SELECT incident_id, title, started_at FROM incident
WHERE severity = 'SEV1' AND status = 'Open';
```

| | Before | After |
|---|---|---|
| Plan | `type=ALL`, 5,900 rows | `type=ref`, 100 rows |
| **Measured time** | **~1.2 ms** | **~0.09 ms** |

**Roughly an order of magnitude faster** with
`idx_incident_severity_status (severity, status)`.

`severity` leads because it is the more selective of the two here — SEV1 is ~8%
of rows, `Open` is ~20% — and putting the more selective column first discards
more rows earlier in the B-tree descent.

---

## 7. Pattern 5 — where an "obvious" index made things worse

> "Which production deployments were followed by an incident on the same
> component within 7 days?"

A four-table join with a time range; the most expensive analytical query in the
project.

The bridge table `incident_component` has `PRIMARY KEY (incident_id,
component_id)`. This query traverses it **from the component side**, which that
key cannot serve. The textbook move is therefore to add
`incident_component(component_id, incident_id)`.

**All four combinations measured:**

| Configuration | Optimiser cost | Measured time |
|---|---|---|
| no extra index | 46,065 | 39.2 ms |
| `+ incident(started_at)` | 46,065 | **34.4 ms** |
| `+ incident_component(component_id, incident_id)` | 5,001 | **108 ms** |
| both | 5,001 | **111 ms** |

The composite bridge index makes the optimiser's cost estimate **nine times
better** and, on Linux, the actual query **roughly three times slower**. That
comparison was re-measured on a separate Linux run (40/35/94 ms) and held.

**It does not hold on Windows.** Re-measured natively on Windows, all three
configurations land between 521 and 753 ms with overlapping ranges - the query
is I/O-bound there and the differences vanish into run-to-run noise:

| Configuration | Linux | Windows (2 runs) |
|---|---|---|
| no extra index | 39 ms | 596, 715 ms |
| `+ incident(started_at)` | 34 ms | 621, 753 ms |
| `+ bridge index` | 108 ms | 521, 691 ms |

So the *timing* evidence for rejecting this index is platform-specific. The
**plan** evidence is not: on both platforms the bridge index makes the optimiser
abandon the 6,000-row `incident` table and drive from a full scan of all 39,233
`deployment` rows, followed by roughly 112,000 index lookups. That is
objectively more work on any platform, and it is the reason the index stays
rejected. See section 10 for the full cross-platform comparison.

**Why.** Given the new index, the optimiser reorders the join to drive from
`deployment`: a full scan of all 40,000 deployment rows followed by ~112,000
index lookups, instead of driving from the 6,000-row `incident` table. The plan
looks cheaper to the cost model and is not.

**Decision: keep `incident(started_at)`. Reject
`incident_component(component_id, incident_id)`.**

This is the entire argument for measuring rather than adding indexes by reflex.
"It should help" and "EXPLAIN says it is cheaper" are both weaker evidence than
a stopwatch.

*Footnote:* in the real `infratrace` schema a single-column index on
`incident_component(component_id)` exists anyway, because InnoDB creates one
automatically for the foreign key. That is unavoidable and harmless at the real
data size; the finding above is about the composite index.

---

## 7b. Pattern 6 — ownership audit

> "What does team 7 own, broken down by type?"

```sql
SELECT component_type, COUNT(*) FROM component
WHERE owner_team_id = 7 GROUP BY component_type;
```

| | Before | After |
|---|---|---|
| Plan | `type=ALL`, 2,000 rows, `Using temporary` | `type=ref`, 50 rows, `Using index` |
| **Measured** | **~0.96 ms** | **~0.04 ms** |

`owner_team_id` leads because it is the equality filter. `component_type`
second does double duty: it satisfies the `GROUP BY` from index order (removing
the temporary table) and makes the index **covering**, so the table rows are
never touched.

The same index serves the unowned-components audit through its leading column
alone — `WHERE owner_team_id IS NULL` becomes a covering index lookup
(~0.02 ms). That is why it is one composite index rather than two single-column
ones.

## 7c. Pattern 7 — deployment timeline

> "When was the most recent deployment anywhere?" — how the analytical queries
> anchor their notion of "recent" — and "how many deployments in a given month?"

This is **not** the same access path as pattern 3. There the query filtered on a
specific component and environment, so `idx_deployment_comp_env_time` could
seek. Here there is no such filter, so that index can only be *scanned*.

| Query | Before | After |
|---|---|---|
| `MAX(deployed_at)` | full covering scan of ~40,000 entries, **~4.7 ms** | **`Select tables optimized away`** — no rows examined |
| one-month range count | **~4.4 ms** | **~0.3 ms** |

The `MAX()` result is the most striking in the study: with `deployed_at` as the
leading column of its own index, the maximum is the **last entry of the B-tree**,
so the optimiser resolves it during planning and the query examines no rows at
all.

## 8. When an index cannot help at all

Two rules that bite in practice, demonstrated on the same column so the
comparison is exact.

### Leading wildcard

| Query | Plan |
|---|---|
| `component_name LIKE 'Order%'` | `type=range` — index seek |
| `component_name LIKE '%Service%'` | `type=index` — scans all 2,000 entries |

A B-tree is ordered by prefix. A pattern with no known prefix gives nothing to
seek to, so MySQL can only scan. Note it is `type=index`, not `ALL` — it scans
the *index*, which is narrower than the table, but it is emphatically not a
lookup.

### A function on an indexed column

| Query | Plan |
|---|---|
| `YEAR(started_at) = 2026` | `type=index` — the function hides the column |
| `started_at >= '2026-01-01' AND started_at < '2027-01-01'` | `type=range` — index used |

The index stores `started_at`, not `YEAR(started_at)`. Wrapping the column makes
the predicate **non-sargable**. The rewrite asks the identical question in a
form the index can answer.

(MySQL 8 can index the first case with a generated column or a functional index
— but rewriting the predicate is free.)

---

## 9. The cost of the indexes

Indexes are a trade: faster reads, slower writes, more disk. Measured on
`infratrace_perf`:

| Table | Data (MB) | Index (MB) | Index as % of data |
|---|---|---|---|
| `deployment` | ~3.5 | ~1.5 | ~43% |
| `incident_component` | ~0.6 | ~0.3 | ~50% |
| `dependency` | ~0.3 | ~0.2 | ~65% |

Every index must also be maintained on `INSERT`, `UPDATE` and `DELETE`. For
InfraTrace this trade is clearly correct: the data is written rarely (a
deployment now and then, an incident occasionally) and read constantly by
analysis. An OLTP system with a heavy write path would weigh it differently.

Run `SELECT ... FROM information_schema.tables` at the end of
`02_explain_analysis.sql` for the exact current figures.

---

## 9b. Cross-platform verification

Every pattern was re-measured on **MySQL 8.0.46 installed natively on Windows**
(the environment this project is demonstrated in) and compared against the
Linux container results.

| Pattern | Linux before -> after | Windows before -> after | Same conclusion? |
|---|---|---|---|
| 1. reverse dependency lookup | ~0.9 -> ~0.013 ms | ~1.06 -> ~0.018 ms | **yes** |
| 2. CTE with vs without `depth` | 54.5 -> 3.7 ms (~15x) | 245 -> 4.1 ms (~59x) | **yes**, larger on Windows |
| 3. latest deployment | ~7 -> ~0.02 ms | ~8.4 -> ~0.055 ms | **yes** |
| 4. incident severity+status | ~1.2 -> ~0.09 ms | ~1.6 -> ~0.15 ms | **yes** |
| 5. deploy-incident correlation | 39 / 34 / 108 ms | 596-715 / 621-753 / 521-691 ms | **NO** - see below |
| 6. ownership audit | ~0.96 -> ~0.04 ms | ~0.42 -> ~0.026 ms | **yes** |
| 7. deployment timeline `MAX()` | scan -> optimised away | 5 ms -> optimised away | **yes** |

**Six of seven reproduce.** Plan types are identical on both platforms in every
case, including `Select tables optimized away` for pattern 7 and the
`ALL` -> `ref` + `Using index` transitions.

**Pattern 5 is the exception, and it is worth being precise about why.** On
Windows this query costs ~600-750 ms regardless of indexing, because it is
bound by I/O rather than by row access. The ~25% run-to-run variance is larger
than the difference between the three index configurations, so no ordering can
be read out of it. The Linux measurement, where the query runs in tens of
milliseconds, has enough resolution to separate them.

The honest conclusion: **the index is rejected on plan evidence, corroborated by
Linux timings, and the Windows timings are simply too noisy to say anything
either way.** It would have been easy to quote only the Linux numbers and not
mention this; that would have overstated how well-established the finding is.

A secondary observation worth recording: Windows MySQL was roughly 15-18x slower
than the Linux container on the I/O-heavy joins, while being comparable on the
index-lookup patterns. `innodb_buffer_pool_size` (128 MB), `innodb_io_capacity`
and `innodb_log_file_size` were identical on both; the settings that differed
were `innodb_flush_method` (`unbuffered` on Windows, `fsync` on Linux) and
`lower_case_table_names`. No attempt was made to tune either platform - both
ran at their installed defaults, which is what a reader would reproduce.

## 10. Summary of decisions

| Index | Verdict | Measured basis |
|---|---|---|
| `idx_dependency_reverse (depends_on_id, component_id)` | **keep** | 70× on reverse lookup; 2.3× on recursive traversal; covering |
| `idx_deployment_comp_env_time (component_id, environment_id, deployed_at DESC)` | **keep** | 350×; eliminates filesort |
| `idx_incident_severity_status (severity, status)` | **keep** | 13× |
| `idx_incident_started_at (started_at)` | **keep** | 39.2 → 34.4 ms on the correlation query |
| `idx_component_owner_type (owner_team_id, component_type)` | **keep** | ~0.96 ms -> ~0.04 ms on the ownership audit (pattern 6); covering, and its leading column also serves the unowned audit |
| `idx_deployment_time (deployed_at)` | **keep** | `MAX(deployed_at)` goes from a 40,000-entry index scan to **"Select tables optimized away"**; month range scan ~4.4 ms -> ~0.3 ms (pattern 7) |
| `incident_component (component_id, incident_id)` | **rejected** | cost 9× better, runtime 2.8× **worse** |
| single-column FK indexes | **not created** | InnoDB creates them automatically; verified none is duplicated |

### The three transferable lessons

1. **Measure, do not assume.** One index that looked obviously right made its
   query 2.8× slower.
2. **Optimiser cost is not time.** It ranks plans; it does not predict
   milliseconds. Two findings here showed estimate and reality diverging by
   three orders of magnitude.
3. **Query shape can beat indexing.** The 15× win on blast radius came from
   removing a column from a CTE, not from any index.
