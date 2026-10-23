# 5-minute demo script

All commands verified working. Outputs below are the actual observed values.

## Setup (before the room, not during)

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" --login-path=infratrace --table infratrace
```
Leaves you at a `mysql>` prompt already inside the database. Have a second
terminal open at the repo root for the Python step.

**Do not run `setup.sql`.** The database is already built and verified.

---

## 0:00–0:30 — Frame it

> "InfraTrace answers one question: if a component fails, what else breaks?
> The database answers it exactly, in SQL. The model answers a harder version —
> *how likely* is each of them to actually fail."

## 0:30–1:30 — The database answer

```sql
CALL sp_get_blast_radius(11);
```

**Expected — exactly 3 rows:**
```
hops_away | affected_component | criticality | team_to_notify
        1 | Payment Service    | Critical    | Payments Team
        2 | Order Service      | Critical    | Commerce Team
        3 | API Gateway        | Critical    | Platform Engineering
```

> "Component 11 is Payment DB. This is a recursive CTE walking the dependency
> graph backwards — joining `depends_on_id` to `component_id` and repeating
> until nothing new appears. Transitive closure in pure SQL, and it tells me
> which three teams to page."

## 1:30–2:30 — Why that isn't enough

```sql
SELECT dependency_type, is_critical, COUNT(*) n FROM dependency
GROUP BY dependency_type, is_critical ORDER BY n DESC;
```

> "Reachability treats every one of these edges as fatal. On our 22 recorded
> incidents it flags 66 components where 37 were actually affected — precision
> 43.9%. And the edges are not equivalent: on the same data a synchronous
> critical dependency carried failure 85.7% of the time, an asynchronous
> non-critical one 40%. The CTE cannot use that, because reachability is a
> boolean property of the graph."

## 2:30–3:30 — The model answer

In the second terminal:

```bash
python -c "
from ml.extract import load_graph
from ml.predict import load_predictor
g=load_graph(); p=load_predictor()
for r in p.predict(g,11): print(r['hops_away'], r['component_name'], r['predicted_probability'])
for e in p.explain(g,11,top_k=3): print(e.source_name,'->',e.target_name,e.transmission_probability)
"
```

**Expected:**
```
1 Payment Service 0.568
2 Order Service   0.290
3 API Gateway     0.221

Payment DB      -> Payment Service  0.734
Order Service   -> API Gateway      0.688
Payment Service -> Order Service    0.664
```

> "Same three components — the model never adds or removes one, that's provable
> from the propagation rule. It scores them. And the explanation is the
> computation itself: Order Service sits at 0.29 because the edge into Payment
> Service transmits at 0.73 and the next one at 0.66. Those are probabilities on
> real dependency edges, not a post-hoc attribution."

## 3:30–4:30 — The evidence

Show the table from `03-RESULTS.md`:

> "We evaluated this on a **real production dependency graph** — 708 services
> from Bitdefender, published with ChronoGraph — using 30 service-disjoint,
> hub-stratified partitions. PR-AUC 0.552, against 0.322 for the SQL
> reachability it replaces and 0.507 for logistic regression on hand-engineered
> path features.
>
> But logistic regression isn't the baseline that matters. The real incumbent is
> a hand-written table of dependency weights — the thing an on-call tool
> actually ships. We ran that too: 0.510, and we beat it by **0.042 in 29 of 30
> partitions**. Then we fitted the same table's **eight weights** on training
> data, and it jumped to **0.538** — which makes it the strongest baseline in
> the whole study, ahead of logistic regression and ahead of GCN and GraphSAGE.
>
> So the honest number is this: against the strongest baseline our margin is
> **0.014, in 27 of 30 partitions** — and that eight-cell table is actually
> *better calibrated* than our model, 0.120 against 0.132, and slightly more
> precise. Two thirds of everything learning buys you here comes from fixing
> eight numbers. The graph-neural model earns the last third, and pays for it in
> calibration.
>
> And the cascades over that topology are simulated — the structure is real, the
> failures are not. These aren't validated predictions of production
> outages."

## 4:30–5:00 — Close on the limits

> "Three things we will not claim. The model is not novel — it's hyperparametric
> Independent Cascade, documented in our prior-art notes. It's one graph from one
> organisation, so this is not generalisation, and we make no significance claim.
> And the study failed two of its six pre-registered acceptance criteria —
> minimum partition gap and calibration — which we report rather than hide.
>
> What we do claim: the SQL baseline is preserved exactly, and we measured what
> learning the edge probabilities is worth against the practice it would
> replace — +0.042 over the hand-written table, +0.014 over the fitted one, at a
> cost in precision and calibration. That's an applied and empirical
> contribution, not a new algorithm. Plus a hard 78.4% ceiling on recall that no
> reverse-traversal method can beat."

---

## If something fails

| problem | fallback |
|---|---|
| Python/model errors | the SQL half stands alone; `sp_get_blast_radius(11)` is the core demo |
| `load_predictor` raises | artifact missing — say so, show `03-RESULTS.md` instead; do not debug live |
| asked for the API | `cd backend && python -m uvicorn app.main:app` then `GET /components/11/blast-radius/predicted`. Takes ~40s to start — only if time allows |

## Backup one-liners (no interactive prompt)

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe" --login-path=infratrace --table infratrace -e "CALL sp_get_blast_radius(11);"
```
