# InfraTrace — Testing

Two suites: **166 database tests** in SQL and **45 API tests** in pytest.
Both run against a real MySQL 8 instance, and both have been run to completion
on **MySQL 8.0.46 natively on Windows** and on **MySQL 8.0.46 in a Linux
container**, with identical results (166/166 and 45/45 on each).

```bash
# database
cd tests && mysql -u root -p --table < run_all_tests.sql

# API
cd backend && python -m pytest tests/ -v
```

---

## 1. What is actually being tested

The guiding principle is that a test should be able to **fail for a real
reason**. Counting objects only proves they were declared; these suites also
prove they *behave*.

| Question | How it is answered |
|---|---|
| Do the objects exist? | suite 01 — counts and names every table, view, routine, trigger, index |
| Do the constraints actually reject bad data? | suite 02 — attempts 21 invalid writes and records whether the database refused |
| Is the loaded data internally consistent? | suite 03 — 28 invariants, including "the graph is acyclic" |
| Do the stored programs return the right values? | suite 04 — expected numbers derived by hand from the dataset |
| Does the analysis return meaningful results? | suite 05 — non-empty checks plus known answers plus fan-out guards |
| Can circular dependencies still slip through? | suite 06 — cycles at 1, 2, 3, 4, 35 and 60 nodes |
| Does the API return what the database computed? | pytest — integration tests against real data |

---

## 2. Database suite

### The harness

MySQL has no assertion mechanism, so `tests/sql/00_harness.sql` creates a
`TEMPORARY` table that every later suite writes one row into:

```sql
CREATE TEMPORARY TABLE test_result (
    id INT AUTO_INCREMENT PRIMARY KEY,
    suite VARCHAR(40), test_name VARCHAR(140),
    expected VARCHAR(80), actual VARCHAR(80), result VARCHAR(4)
);
```

Every test follows the same shape:

```sql
INSERT INTO test_result (suite, test_name, expected, actual, result)
SELECT 'integrity', 'dependency rows', '29', COUNT(*),
       IF(COUNT(*) = 29, 'PASS', 'FAIL')
FROM dependency;
```

The table is temporary, so it lives only for the session that runs the suite and
leaves nothing behind — which is why `run_all_tests.sql` `SOURCE`s all files in
**one** session.

`99_summary.sql` then prints per-suite totals, every failure in full, and a
single verdict line:

```
+-------------+--------+--------+------------------+
| total_tests | passed | failed | verdict          |
+-------------+--------+--------+------------------+
|         166 |    166 |      0 | ALL TESTS PASSED |
+-------------+--------+--------+------------------+
```

### Suite 01 — schema objects (37 tests)

Counts 10 tables, 4 views, 5 procedures, 6 functions, 5 triggers; then checks
each by **name**, so renaming something is caught rather than silently
compensated by a count that still adds up. Also verifies the six explicit
indexes exist.

One test guards a specific past defect:

```sql
-- functions must NOT be declared DETERMINISTIC
SELECT 'no function falsely marked DETERMINISTIC', '0', COUNT(*) ...
WHERE routine_type = 'FUNCTION' AND is_deterministic = 'YES';
```

They read tables, so `DETERMINISTIC` would be a false declaration and unsafe
with statement-based binary logging. All six were originally mis-declared.

### Suite 02 — constraints actually fire (25 tests)

The most valuable suite. Each test runs a statement that is **supposed to fail**
inside a procedure with a `CONTINUE HANDLER`, so a rejection is captured as a
PASS rather than aborting the run:

```sql
CREATE PROCEDURE test_expect_rejection(IN p_label VARCHAR(140), IN p_sql TEXT)
BEGIN
    DECLARE v_rejected TINYINT DEFAULT 0;
    DECLARE CONTINUE HANDLER FOR SQLEXCEPTION SET v_rejected = 1;
    START TRANSACTION;
      SET @stmt = p_sql; PREPARE s FROM @stmt; EXECUTE s; DEALLOCATE PREPARE s;
    ROLLBACK;                       -- never keep the row, even if wrongly accepted
    INSERT INTO test_result ... IF(v_rejected = 1, 'PASS', 'FAIL');
END
```

Covered: 6 CHECK constraints, 4 UNIQUE, 3 FOREIGN KEY, 1 NOT NULL, and 7
trigger rules — including transitive cycles at three and four nodes, and the
`UPDATE` paths that a rule could otherwise be bypassed through.

Four further tests then confirm the dataset row counts are **unchanged**, so a
constraint that wrongly accepted a row would be caught even if the rejection
check somehow passed.

### Suite 03 — data integrity (28 tests)

Row counts for all 10 tables, plus semantic invariants that foreign keys alone
cannot express:

- every incident has **exactly one** `RootCause` component
- no resolved incident is missing `resolved_at`, and no open incident carries one
- a confirmed causing deployment belongs to a component the incident affected,
  **and** precedes it
- no retired component is deployed to production
- **the dependency graph is acyclic** — a recursive CTE walks from every
  component and counts walks that return to their origin. Its depth bound is the
  component count, which is the exact bound for a simple cycle

Plus explicit **edge-case** checks, because the dataset's value depends on them:
2 unowned components, 1 retired component, 3 open incidents, all three
environments used, all four severities used.

### Suite 04 — stored programs return correct values (34 tests)

Expected numbers derived by hand from the seed data, so a wrong refactor of a
recursive query is caught rather than silently accepted:

| Assertion | Expected |
|---|---|
| `fn_blast_radius_count(Payment DB)` | 3 |
| `fn_blast_radius_count(Kafka)` | 7 |
| `fn_dependency_depth(API Gateway)` | 3 |
| `fn_risk_score(Kafka)` | 28 |
| `fn_would_create_cycle(11, 3)` — transitive | 1 |
| `fn_would_create_cycle(6, 15)` — safe edge | 0 |

Views are checked for the invariant that matters most: **a view must never
multiply rows**. And one test guards a real bug that shipped:

```sql
-- Inventory Service was rolled back to v3.9.0 after incident 5.
-- The view must report what is RUNNING, not the version that was undone.
SELECT 'production view honours rollback', 'v3.9.0', running_version, ...
```

### Suite 05 — analysis is meaningful (22 tests)

Non-empty checks (the dataset's whole purpose is that the analysis is not
empty), known-correct answers, and **fan-out guards**.

The fan-out guards exist because of a real bug: the platform summary once joined
`dependency`, `incident_component` and `deployment` in one query, which
multiplied rows and made it report **182 critical components instead of 8**. The
tests now assert the per-type counts still sum to the true totals.

### Suite 06 — cycle-prevention regression (20 tests)

This suite exists because of a specific defect an audit found and reproduced.

Cycle prevention was bounded at 50 hops in `fn_would_create_cycle` and 20 in
`sp_detect_dependency_cycles`. With a 60-component chain, the edge closing a
60-node cycle was **accepted**, a real cycle was stored, and the detector did
**not** find it — while the documentation claimed prevention "of any length".

The suite rebuilds that exact scenario:

| Group | Tests |
|---|---|
| Short cycles on the real graph | 1-node (self), 2-node (mutual), 3-node, 4-node — all rejected |
| Function agreement | `fn_would_create_cycle` returns 1 for all four, and **0 for a safe edge** |
| The 60-node chain | builds 9001→…→9060, asserts depth 59, asserts the function sees a 59-hop closing path |
| The regression itself | closing edges at **60 nodes** and **35 nodes** are both rejected |
| Detection independent of prevention | drops the INSERT trigger, injects a real cycle, asserts the detector finds all 60 participants and reports length 60 |
| Termination | blast radius still terminates on cyclic data (`UNION` de-duplicates) |
| Cleanup | row counts restored, all five triggers restored, prevention live again, graph acyclic |

The trigger is restored by `SOURCE`-ing the canonical `triggers.sql` rather than
re-typing its body, so the test cannot drift from the shipped definition.

### Scenario-existence tests (in suite 05)

A second audit finding was that query A4 had gone **silently empty** — the
dataset had been extended until every component had an incident, so
"components that never had an incident" returned nothing, and no test noticed
because **the test suite never executes the query files**.

Suite 05 now asserts that each demonstration scenario still exists: a deployed
component with no incident history (and one with a wide blast radius), an
ownership gap, cross-team dependency edges, a failed or rolled-back deployment,
and a component beating its type average. These do not run the query files, but
they do fail if the data a query depends on disappears.

---

## 3. API suite (45 tests)

`backend/tests/test_api.py`, using FastAPI's `TestClient`.

**Integration, deliberately.** The point of the API is that it returns what the
database computes, so mocking the database away would test nothing worth
testing. Each test asserts on real values from the ShopSphere dataset.

**Graceful skip.** If the database is unreachable or unloaded, every test skips
with a clear message rather than failing — an unconfigured machine is not a
broken API:

```python
@pytest.fixture(scope="session")
def db_available():
    try:
        info = ping()
    except Exception as exc:
        pytest.skip(f"Database not reachable - configure backend/.env first ({exc})")
    if info.get("components", 0) < 19:
        pytest.skip("Schema present but seed data missing - run database/data/seed.sql")
```

### What is covered

| Area | Examples |
|---|---|
| Health | reports 10 tables, 4 views, 11 routines |
| Components | full estate, ordering by risk, each filter, 404 and 422 paths |
| Dependencies | Order Service's exact five; recursive reaches Payment DB and Stripe |
| **Blast radius** | the exact chain `Payment Service → Order Service → API Gateway`; all four applications; the propagation path string; **that the caveat is present and mentions redundancy, failover and degradation** |
| Impact report | all five sections present and correctly mapped |
| Incidents | severity filter returns 6 SEV1; the three open ones; root cause first |
| Teams | component counts sum to 17, and Mobile Team (owning none) is still listed |
| Analytics | summary matches the dataset exactly; Kafka leads the risk ranking; retired components excluded |
| **Rollback** | production infrastructure reports Inventory Service as `v3.9.0` |
| Correlation | `confirmed + correlation_only == count`, both non-zero |
| Graph | 19 nodes, 29 edges, every edge references a real node, graph is acyclic |
| **Security** | 6 injection payloads; API is read-only |
| **JSON types** | aggregates are real numbers, not quoted strings |

### Three tests that exist because of real bugs

1. `test_teams_list_has_no_fan_out_inflation` — the teams query originally
   joined developer, component and application together, inflating every count.
2. `test_impact_report_returns_all_five_sections` — `call_proc` originally
   skipped **empty** result sets, so a component with no dependencies shifted
   every later section up by one and the caller read applications as the blast
   radius.
3. `test_numeric_aggregates_are_json_numbers_not_strings` — MySQL returns
   `DECIMAL` for `SUM`/`AVG`/window results, which FastAPI serialised as
   `"22"` instead of `22`.

---

## 4. Test data safety

No suite modifies the dataset.

| Suite | Protection |
|---|---|
| DB suite 02 | every attempted write is inside a transaction that is rolled back; four tests then confirm the row counts |
| DB suites 01, 03, 04, 05 | read-only |
| API suite | the API has no write endpoints, and its database account has no write grant |
| Transaction demos | operate on dedicated rows (ids 9001+); `99_demo_teardown.sql` removes them and **verifies** all ten row counts plus three invariants |
| Performance suite | builds a **separate** schema `infratrace_perf`; never touches `infratrace` |

---

## 5. Known gaps

Stated plainly rather than left to be discovered:

- **No CI pipeline.** Both suites are run by hand. A GitHub Actions workflow
  with a MySQL service container is the obvious next step.
- **No coverage measurement** on the Python code.
- **Durability is not tested.** Doing it honestly means killing the server
  mid-transaction and verifying recovery.
- **Concurrency demos are verified by inspection**, not asserted. The automated
  runner reproduces every scenario with real concurrent connections and prints
  the results, but it does not fail a build if the behaviour changes.
- **The frontend has no automated test suite.** It was verified in a real
  headless browser (all views render, no console errors, filters work, no
  horizontal overflow at 420px), but those checks are not committed as a
  repeatable suite.
- **The query files are still not executed by the test suite.** Suite 05 now
  asserts the *scenarios* those queries depend on, which is what caught the A4
  regression, but a syntax error introduced into `database/queries/*.sql` would
  still not fail the suite. Running each file and asserting a non-empty result
  set is the obvious next step.
- **Expected values are hardcoded to the seed dataset.** Changing the seed data
  requires updating the tests — which is a feature at this size (the suite
  caught nine stale expectations when the dataset was extended) but would not
  scale to a large evolving dataset.
