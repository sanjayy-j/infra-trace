# InfraTrace — Transactions and Concurrency

Every result in this document was produced by running two **genuinely concurrent
connections** against MySQL 8.0.46. Nothing here is hypothetical.

Reproduce all of it:

```bash
cd database
mysql -u root -p infratrace < transactions/00_demo_setup.sql
sh transactions/run_concurrency_demos.sh
mysql -u root -p --table infratrace < transactions/99_demo_teardown.sql
```

The demos operate on **dedicated demo rows** (ids 9001+) using the real schema,
constraints and triggers. The seeded ShopSphere dataset is never modified — the
teardown script proves it, checking every table's row count and a set of
invariants.

---

## 1. ACID

### Atomicity — all of it, or none of it

Promoting a release is really two writes that belong together:

1. record the production deployment
2. close the incident the release fixes

If (2) fails, (1) must not survive — otherwise the database claims a release
went out to fix an incident that is still open.

`01_acid_basics.sql` §3 makes (2) fail deliberately (`'Closed'` is not a valid
`incident.status`, so `chk_incident_status` rejects it):

```sql
DECLARE EXIT HANDLER FOR SQLEXCEPTION
BEGIN
    ROLLBACK;
    SELECT 'second statement failed -> ROLLBACK -> neither write survives';
END;

START TRANSACTION;
    INSERT INTO deployment (...) VALUES (9001, 3, 'v1.1.0', ...);   -- succeeds
    UPDATE incident SET status = 'Closed' WHERE incident_id = 9001;  -- FAILS
COMMIT;
```

**Measured result:**

```
deployments_after | incident_after | verdict
                2 | Open           | ATOMICITY HELD
```

The successful first write was undone along with the failed second one.

### Consistency

14 `CHECK` constraints, 15 foreign keys, 8 `UNIQUE` constraints and 5 triggers
mean no transaction can commit a state that breaks a rule — including the
non-local rule "the dependency graph contains no cycle", which the triggers
enforce via `fn_would_create_cycle()`.

### Isolation

Four levels, demonstrated in §2 below.

### Durability

Provided by InnoDB's redo log and `innodb_flush_log_at_trx_commit`. Once
`COMMIT` returns, the change survives a crash.

**Not separately demonstrated**, and worth being straight about why:
demonstrating durability honestly means killing the server mid-transaction and
verifying recovery, which is beyond what this project sets up. The mechanism is
InnoDB's, not something InfraTrace implements.

---

## 2. Isolation levels

MySQL's default is `REPEATABLE READ`. All four results below are measured.

### Non-repeatable read at READ COMMITTED

| Step | Session A | Session B |
|---|---|---|
| 1 | `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED; START TRANSACTION;` `SELECT criticality` → **High** | |
| 2 | | `UPDATE component SET criticality='Critical'` (autocommits) |
| 3 | `SELECT criticality` → **Critical** | |

The value **changed inside a single transaction**. Any calculation A made from
the first read is now based on stale data.

### Repeatable read at REPEATABLE READ

| Step | Session A | Session B |
|---|---|---|
| 1 | `START TRANSACTION;` `SELECT criticality` → **High** | |
| 2 | | `UPDATE ... 'Critical'` (committed) |
| 3 | `SELECT criticality` → **High** | |
| 4 | `COMMIT;` then `SELECT` → **Critical** | |

A keeps a **consistent snapshot** taken at its first read, released on commit.

This is why the default matters to InfraTrace specifically: a blast-radius
traversal issues several recursive reads. If another session committed a new
dependency edge halfway through, a weaker level would let the traversal mix old
and new graph states and report an impact set that never actually existed.

### Dirty read at READ UNCOMMITTED

| Step | Session A | Session B |
|---|---|---|
| 1 | `START TRANSACTION;` `UPDATE ... SET criticality='Low'` — **not committed** | |
| 2 | | `SET ... READ UNCOMMITTED;` `SELECT criticality` → **Low** |
| 3 | `ROLLBACK;` | |
| 4 | | `SELECT criticality` → **High** |

B read a value that **never existed** in any committed state.

### Phantom rows at REPEATABLE READ

| Step | Session A | Session B |
|---|---|---|
| 1 | `START TRANSACTION;` `SELECT COUNT(*) ... owner_team_id=2` → **5** | |
| 2 | | `INSERT` a new component owned by team 2 (committed) |
| 3 | `SELECT COUNT(*)` → **5** | |
| 4 | `COMMIT;` then `SELECT COUNT(*)` → **6** | |

### What each level prevents

| Level | Dirty read | Non-repeatable read | Phantom |
|---|---|---|---|
| READ UNCOMMITTED | possible | possible | possible |
| READ COMMITTED | prevented | possible | possible |
| **REPEATABLE READ** (default) | prevented | prevented | prevented\* |
| SERIALIZABLE | prevented | prevented | prevented |

\* InnoDB prevents phantoms at REPEATABLE READ using its MVCC snapshot for
plain reads and next-key (gap) locking for locking reads. The SQL standard only
guarantees this at SERIALIZABLE, so InnoDB is **stricter than the standard** —
a detail often stated incorrectly.

---

## 3. The lost update

The most important concurrency bug in this project, because it involves **no
error at all**.

### The scenario

Incident 9001 is open. Two responders investigate in parallel and each records
what they found in `incident.root_cause`.

| Step | Session A (Sneha) | Session B (Vikram) |
|---|---|---|
| 1 | `SELECT root_cause` → **NULL** | |
| 2 | | `SELECT root_cause` → **NULL** |
| 3 | `UPDATE ... 'Connection pool exhausted under load.'` `COMMIT` | |
| 4 | | `UPDATE ... 'Replica disk I/O saturated.'` `COMMIT` |

**Measured final value:** `Replica disk I/O saturated.`

Sneha's finding is **gone**. Both transactions committed successfully. No
constraint was violated. The database is not corrupt — the data is simply
wrong, because the application logic assumed nothing else would write between
its read and its write.

### Why REPEATABLE READ does not save you

A common misreading is "MySQL defaults to REPEATABLE READ, so I am safe." The
demo above **runs at REPEATABLE READ and still loses the update**. Repeatable
read guarantees your *reads* are consistent within a transaction. It says
nothing about whether your *write* is based on a value that is still current.

### Cure 1 — a locking read

```sql
SELECT root_cause FROM incident WHERE incident_id = 9001 FOR UPDATE;
```

`FOR UPDATE` takes an exclusive row lock at **read** time, not write time. The
second session **blocks on its SELECT** until the first commits, then reads the
current value.

**Measured:**

```
A reads (FOR UPDATE): NULL
B reads (FOR UPDATE, after waiting for A): Connection pool exhausted under load.
final: Connection pool exhausted under load. Also: replica disk I/O saturated.
```

Both findings survive.

### Cure 2 — make the write atomic

The lost update exists because the value travelled out to the client and back.
Compute the new value **inside** the `UPDATE` and no window exists:

```sql
UPDATE incident
   SET root_cause = CONCAT(COALESCE(root_cause, ''), 'Connection pool exhausted. ')
 WHERE incident_id = 9001;
```

InnoDB takes the row lock for the duration of the statement automatically. Both
sessions can run in any order and both fragments survive.

### Cure 3 — optimistic concurrency

Add a version column and write `UPDATE ... WHERE version = <the one I read>`,
then check the affected row count. Zero rows means somebody else won; re-read
and retry. Better than locking under low contention, because no lock is held
across the think time.

### Which InfraTrace operations are actually at risk

Most InfraTrace writes are **append-only inserts** — deployments, incidents,
dependency edges — which do not suffer this problem at all. The vulnerable
operations are the in-place edits:

- `incident.root_cause` and `incident.status`
- `component.criticality` and `component.owner_team_id`

Those are exactly where a locking read belongs.

---

## 4. Locking behaviour

All measured.

### InnoDB locks rows, not tables

| Action | Result |
|---|---|
| A: `SELECT ... WHERE component_id = 9001 FOR UPDATE` | acquires |
| B: `SELECT ... WHERE component_id = 9002 FOR UPDATE` | **returns immediately** |
| B: `SELECT ... WHERE component_id = 9001 FOR UPDATE` | **blocks** until A commits |

Two engineers changing different components never block each other.

### Readers are never blocked by writers

With A holding an uncommitted `UPDATE` on component 9001:

| B runs | Result |
|---|---|
| `SELECT criticality ...` (plain) | **returns instantly** with the last committed value |
| `SELECT fn_blast_radius_count(9002)` | **runs normally** |
| `SELECT ... FOR SHARE` | **blocks** — a locking read needs the real current row |

This is the practical value of MVCC: the whole read-only analysis layer —
dashboards, blast radius, reports — keeps working at full speed while engineers
are writing.

### Lock wait timeout

```
ERROR 1205 (HY000): Lock wait timeout exceeded; try restarting transaction
```

After `innodb_lock_wait_timeout` seconds (default 50). Note the advice in the
message: a timeout is a **transient** failure, so application code should roll
back and retry rather than surfacing a permanent error.

### NOWAIT and SKIP LOCKED (MySQL 8)

| Clause | Behaviour | Use |
|---|---|---|
| `FOR UPDATE NOWAIT` | `ERROR 3572` immediately instead of blocking | tell a UI user "someone else is editing this" straight away |
| `FOR UPDATE SKIP LOCKED` | returns only unlocked rows | work queues several workers can drain in parallel |

**Measured:** with A holding 9001, `SKIP LOCKED` over `(9001, 9002)` returned
only 9002.

### Inspecting locks when something is stuck

```sql
SELECT * FROM sys.innodb_lock_waits;                    -- who waits for whom
SELECT * FROM information_schema.innodb_trx;            -- open transactions
SELECT * FROM performance_schema.data_locks;            -- individual locks
```

---

## 5. Deadlock

### Producing one

Two engineers each reassign a **pair** of components, and happen to process the
pair in opposite orders:

| Step | Session A | Session B |
|---|---|---|
| 1 | `UPDATE ... WHERE component_id = 9001` | |
| 2 | | `UPDATE ... WHERE component_id = 9002` |
| 3 | `UPDATE ... WHERE component_id = 9002` — **blocks** | |
| 4 | | `UPDATE ... WHERE component_id = 9001` — closes the cycle |

**Measured:**

```
[B] ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction
lock_deadlocks counter: 1 -> 2
```

A deadlock is not a slow query and not a timeout. It is a **cycle in the
waits-for graph**: A holds what B wants and B holds what A wants. Waiting longer
cannot help, so InnoDB detects the cycle immediately and kills one transaction —
the *victim* — rolling it back **entirely**, not just the one statement.

> A pleasing symmetry for this project: InnoDB's deadlock detector and
> `sp_detect_dependency_cycles()` do the same job — find a cycle in a directed
> graph. One walks *transaction waits for transaction*; the other walks
> *component depends on component*.

### Reading the post-mortem

`SHOW ENGINE INNODB STATUS` has a `LATEST DETECTED DEADLOCK` section naming both
transactions, the exact statement each was running, the locks each held and
wanted, and which was rolled back. Measured excerpt:

```
*** (1) TRANSACTION:
TRANSACTION 4768, ACTIVE 3 sec starting index read
UPDATE component SET criticality='Low' WHERE component_id=9002
*** (1) HOLDS THE LOCK(S):
RECORD LOCKS ... index PRIMARY of table `infratrace`.`component` lock_mode X
*** (1) WAITING FOR THIS LOCK TO BE GRANTED:
RECORD LOCKS ... lock_mode X locks rec but not gap waiting
*** (2) TRANSACTION:
TRANSACTION 4769, ACTIVE 2 sec starting index read
UPDATE component SET criticality='Low' WHERE component_id=9001
```

**Counting deadlocks in MySQL 8:**

```sql
SELECT name, count FROM information_schema.innodb_metrics
WHERE name IN ('lock_deadlocks', 'lock_deadlock_false_positives');
```

`SHOW GLOBAL STATUS LIKE 'innodb_deadlocks'` does **not** exist in MySQL 8 —
that variable is MariaDB/Percona only. (This project's demo originally used it
and returned nothing; `innodb_metrics` is the portable answer.)

### Fix 1 — consistent lock ordering

If every transaction acquires locks in the same order, a two-transaction
deadlock becomes impossible. Ordering by primary key is the simplest rule that
is easy to follow everywhere.

**Measured:** with both sessions going 9001 then 9002, B simply **waits** at its
first statement and then proceeds. Both commit. No 1213.

### Fix 2 — retry, always

Deadlock is transient, so the correct response is to retry the whole
transaction:

```sql
DECLARE CONTINUE HANDLER FOR SQLSTATE '40001' SET v_deadlock = 1;

retry_loop: WHILE v_done = 0 AND v_attempt < v_max_tries DO
    START TRANSACTION;
    UPDATE component SET criticality = p_new WHERE component_id = p_id;
    IF v_deadlock = 1 THEN
        ROLLBACK;
        DO SLEEP(0.1);          -- back off
    ELSE
        COMMIT; SET v_done = 1;
    END IF;
END WHILE;
```

`SQLSTATE '40001'` covers **both** retryable concurrency failures: deadlock
(1213) and lock wait timeout (1205).

### Reducing deadlocks, in order of effectiveness

1. **Consistent lock ordering** everywhere.
2. **Short transactions** — never hold locks across user think time or a network
   round trip.
3. **Index-driven `UPDATE`/`DELETE`.** A full scan locks far more rows than
   intended, so the indexes in `database/performance/` pay off a second time as
   a concurrency improvement.
4. **Always retry.** Deadlocks cannot be eliminated in a concurrent system, so
   handling them is mandatory, not optional.

---

## 6. How this maps back to InfraTrace's design

| Design choice | Concurrency consequence |
|---|---|
| The API is **read-only** | It cannot lose an update or deadlock. It also never blocks writers, because plain `SELECT`s read the MVCC snapshot. |
| Most writes are **append-only** | Inserting a deployment or incident contends with nothing. |
| In-place edits are few and identified | `incident.root_cause`, `incident.status`, `component.criticality`, `component.owner_team_id` — the places to use `FOR UPDATE`. |
| `autocommit` left on in the API | No transaction is left open holding row locks across a HTTP response. |
| Analysis lives in **stored programs** | The traversal runs entirely server-side in one round trip, so its read snapshot is short-lived. |

---

## 7. Files

| File | Sessions | Contents |
|---|---|---|
| `00_demo_setup.sql` | 1 | creates demo rows 9001+ |
| `01_acid_basics.sql` | 1 | COMMIT, ROLLBACK, atomicity, SAVEPOINT |
| `02_isolation_levels.sql` | 2 | all four levels, dirty read, phantoms |
| `03_lost_update.sql` | 2 | the bug and three cures |
| `04_row_locking.sql` | 2 | row locks, MVCC, timeout, NOWAIT, SKIP LOCKED |
| `05_deadlock.sql` | 2 | deadlock, post-mortem, ordering fix, retry pattern |
| `run_concurrency_demos.sh` | automated | drives all of the above with real concurrent connections |
| `99_demo_teardown.sql` | 1 | removes demo rows and **proves** the dataset is unchanged |
