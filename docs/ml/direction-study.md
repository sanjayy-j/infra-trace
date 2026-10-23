# Research assessment — directionality in blast-radius prediction

Written before implementation, so the plan can be judged separately from the
results. Results are appended in §7 once the experiments were run.

---

## 1. Verifying the eight forward-propagation impacts

[experiments.md](experiments.md) claimed 8 of 37 recorded impacts travel
*forwards* along a dependency edge. Checking each against the incident record
and the dependency table, that claim is **mostly right but was overstated**.

| inc | root cause | impacted | forward edge exists? | recorded root cause | what actually happened |
|---|---|---|---|---|---|
| 9 | Recommendation Service | Kafka Event Bus | yes (Async) | "Model artefact schema changed without a matching consumer update" | producer wrote incompatible messages onto the bus |
| 11 | API Gateway | Order Service | yes (Sync, critical) | "v1.9.2 applied the partner rate limit to logged-in customers" | gateway **rejected** traffic |
| 11 | API Gateway | Search Service | yes (Sync) | same | same |
| 14 | Authentication Service | Redis Cache | yes (Sync) | "token-cache key change invalidated every active session" | caller churned the cache |
| 15 | Notification Service | Kafka Event Bus | yes (Async, critical) | "consumer group rebalanced and never subscribed" | consumer stalled, bus backed up |
| 18 | Payment Service | Payment DB | yes (Sync, critical) | "idempotency keys not persisted before calling the provider" | retry storm |
| 18 | Payment Service | Stripe Payment API | yes (Sync, critical) | same | retry storm |
| 22 | Shipping Service | Redis Cache | **no edge at all** | "shipment status cache was never invalidated" | — |

Three distinct mechanisms, not one:

- **Forward propagation (5).** Incidents 9, 14, 15, 18×2. A component damages
  something it depends on: producer pollution, cache churn, consumer stall,
  retry storm. These are genuine failure propagation along a forward edge and
  are exactly what the systems literature calls retry/load amplification.
- **Capability attribution (2).** Incident 11. The gateway *rejected* traffic,
  so Order Service and Search Service received **less** load, not more. They
  were almost certainly healthy. What was degraded was the user-visible
  capability those components front. The impact record is attributing
  business impact, not component health. A forward channel would still
  predict these, but the causal story in experiments.md was wrong.
- **Missing dependency edge (1).** Incident 22 names a "shipment status
  cache", and Redis Cache is the cache — but no `dependency` row records
  Shipping Service using Redis. This is not a direction problem at all; it is
  an incomplete graph.

**Correction to [experiments.md](experiments.md):** "8 forward-propagation
impacts" should read "7 impacts reachable only by following a dependency edge
forwards, of which 5 are forward propagation proper, 2 are capability
attribution; 1 further impact is unreachable in either direction because the
graph is missing an edge."

The modelling consequence is unchanged for 7 of 8, which is what matters for
the experiment below.

## 2. What directionality is worth, measured

Reachability under three traversal rules, on the 22 real incidents:

| traversal | flagged | actual | TP | precision | recall |
|---|---|---|---|---|---|
| reverse only (today) | 66 | 37 | 29 | **43.9%** | **78.4%** |
| reverse + 1 forward hop | 223 | 37 | 36 | 16.1% | **97.3%** |
| fully bidirectional | 396 | 37 | 37 | 9.3% | 100% |

A single forward hop recovers 7 of the 8 missing impacts and lifts the recall
ceiling from 78.4% to 97.3% — but it inflates the candidate set 3.4× and
collapses precision to 16.1%. Full bidirectional traversal reaches everything
and is operationally useless.

**This is the research question, and it is well posed:** can a learned,
direction-specific transmission function keep most of the recall that forward
edges unlock while recovering the precision that adding them destroys?

### The signal exists

1-hop propagation rates on the real incidents, split by direction:

| direction | exposures | propagated | rate |
|---|---|---|---|
| reverse (something that depends on the root) | 42 | 26 | **61.9%** |
| forward (something the root depends on) | 22 | 7 | **31.8%** |

Forward edges carry failure at roughly **half** the rate of reverse ones, and
within the forward direction the semantics still order sensibly
(Sync-critical 37.5%, Sync 33.3%, Async 25%, Data 0/3). SCP-GNN currently
assigns every forward edge probability **exactly zero** — they are not in its
propagation graph — so it is structurally wrong on about a third of forward
exposures.

## 3. Prior art

**The mechanism is established and is not claimed as novel.**

| work | relevance |
|---|---|
| **Dir-GNN**, Rossi et al. 2023, [arXiv:2305.10498](https://arxiv.org/abs/2305.10498) | Extends any MPNN with **separate aggregation of incoming and outgoing edges**; proves Directed-WL expressivity. This is precisely the proposed mechanism. |
| **R-GCN**, Schlichtkrull et al. 2018 | Models inverse relations as distinct relation types — direction-specific weights by construction. |
| **CompGCN**, [arXiv:1911.03082](https://arxiv.org/abs/1911.03082) | Composition-based multi-relational GCN, handles inverse relations explicitly. |
| Asymmetric Graph Representation Learning ([arXiv:2110.07436](https://arxiv.org/abs/2110.07436)), DiRW, Directed Homophily-Aware GNN, and many others | A large, active literature on directed GNNs. |

**The phenomenon is also known**, in the systems literature rather than the
graph-learning one:

| work | relevance |
|---|---|
| Retry Amplification in Distributed Systems ([arXiv:2608.25403](https://arxiv.org/abs/2608.25403)) | Systematic analysis of retry policies causing cascading failure — the mechanism behind incident 18. |
| RetryGuard ([arXiv:2511.23278](https://arxiv.org/abs/2511.23278)) | Retry storms in microservices and how to damp them. |
| **AID** ([arXiv:2109.04893](https://arxiv.org/abs/2109.04893)) | Predicts *intensity of dependency* — how much a callee's status influences its caller. Closest predictive work, but **reverse direction only**, from tracing time series rather than graph structure. |
| **ErrorPrism** ([arXiv:2509.26463](https://arxiv.org/abs/2509.26463)) | Reconstructs error propagation paths from logs via static analysis + an LLM. Post-hoc forensics on one failure, not prediction. |

### Where that leaves the contribution

Neither a direction-aware GNN nor forward failure propagation is new. What I
could not find is either of these:

1. a **quantification** of forward versus reverse propagation rates on a
   corpus of recorded incidents against a declared dependency graph;
2. use of **direction-specific transmission** for *predictive* blast radius —
   AID is reverse-only and ErrorPrism is retrospective.

So any contribution here is **empirical, not algorithmic**, and should be
described that way from the start. This is the same conclusion as
[novelty.md](novelty.md), reached before building rather than after.

## 4. Alternatives considered

| direction | why not chosen |
|---|---|
| **Uncertainty-aware propagation** (predictive intervals, deep ensembles) | Worth doing, but with 22 incidents the evaluation of an uncertainty estimate is weaker than the evaluation of a point prediction. Calibration is already measured and already repairable with isotonic regression. Lower information gain per unit of work. |
| **Learning from sparse incidents** (meta-learning, few-shot) | The current model already needs almost no data — 28 parameters suffice. Sparsity is not the binding constraint; *direction* is. |
| **Robustness to incomplete graphs** | Incident 22 shows this is a real problem, and it is the natural follow-up. But it needs a corruption model and a way to tell "missing edge" from "no edge", and the directionality gap is larger and directly measurable now. Noted as next work. |
| **Temporal modelling** | `started_at`/`resolved_at` exist, but the target would change from "which components" to "in what order", which is not comparable to the current baselines. A separate project. |

## 5. Proposed mechanism — the smallest defensible change

Keep everything about SCP-GNN. Add one binary feature.

The propagation graph becomes bidirectional: for each dependency row
`(u depends on v)` emit **two** propagation edges,

```
reverse   v → u    direction flag 0      (dependency fails, dependent suffers)
forward   u → v    direction flag 1      (dependent fails, dependency suffers)
```

and append the flag to the edge feature vector:

```
p_e = σ( MLP_θ( [ x_s ; x_t ; z_e ; dir_e ] ) )
```

Composition is unchanged — the same noisy-OR fixed point. **The entire
modification is one extra input dimension**, which lets a single shared
transmission function learn different probabilities for the two directions.
No new layers, no new loss, no new hyperparameters.

Working name: **SCP-BiD**. SCP-GNN is kept untouched as the baseline.

### What this costs

The reachability bound still holds, but against the *bidirectional* reachable
set, which is 6× larger (396 vs 66 on the real graph). The safety property
"cannot flag anything the SQL would not" is therefore **lost** — the whole
point is to flag things reverse reachability misses. That is a real
regression in the operational argument and must be reported as one.

## 6. Experimental plan

**Models.** Reachability (reverse) · reachability (bidirectional) · depth
decay · logistic on path features · GCN · GraphSAGE · GAT · SCP-GNN
(reverse-only baseline) · SCP-BiD (proposed).

**The two ablations that carry the argument.**

| ablation | isolates |
|---|---|
| SCP-BiD **without** the direction flag (bidirectional edges, one shared function) | whether *direction-specificity* matters, or merely having more edges |
| SCP-GNN (reverse only) | whether forward edges help at all |

Comparing the proposed model only against reverse-only SCP-GNN would conflate
"more edges" with "direction matters", so the middle ablation is the load-bearing one.

**Fairness of the synthetic ground truth.** The current generator simulates
reverse propagation only. Evaluating a bidirectional model on it would rig the
experiment *against* the new model, so the generator must gain a forward
mechanism. To avoid rigging it the other way, the forward mechanism is made
**different in kind** from the reverse one: forward transmission is
**load-driven**, scaling with how many dependents the failing component has
(a heavily-used service generates more retry pressure). That is a function of
node state, not of each edge independently, so it sits outside noisy-OR's
hypothesis class — as the quorum nodes already do for the reverse direction.
Forward base rates are set from the measured real aggregate (~32%, roughly
half of reverse, with Data ≈ 0).

**Metrics.** PR-AUC, precision, recall, F1, ECE before and after isotonic
calibration, parameters, inference latency. 10 seeds, split by graph,
paired tests.

**Real data.** The 22 incidents stay held out. They are the only
non-self-generated evidence, and they are the only place the forward
phenomenon is real rather than assumed. n = 22 with 7 forward-dependent
impacts — enough to detect a recall change, nowhere near enough to settle
precision. Bootstrap CIs over whole incidents.

**Pre-registered success criterion.** SCP-BiD is an improvement only if it
beats reverse-only SCP-GNN on PR-AUC **and** F1 on the real incidents, with
the direction-flag ablation showing the gain comes from directionality rather
than from extra edges. A recall gain paid for entirely in precision is not an
improvement and will be reported as a negative result.

---

## 7. Results

**SCP-BiD fails the pre-registered criterion.** It was to beat reverse-only
SCP-GNN on PR-AUC *and* F1. It beats it on PR-AUC and loses on F1, on both
synthetic and real data. It is not a replacement for SCP-GNN and has not been
deployed.

### Synthetic, 10 seeds, 40 unseen topologies, forward propagation ON

All models scored on the bidirectional candidate set.

| model | PR-AUC | precision | recall | F1 |
|---|---|---|---|---|
| Reachability reverse (SQL today) | 0.244 ±0.014 | 0.301 | 0.635 | 0.408 |
| Reachability bidirectional | 0.145 ±0.010 | 0.145 | 1.000 | 0.253 |
| Depth decay (reverse) | 0.291 ±0.015 | 0.000 | 0.000 | 0.000 |
| Logistic reverse features | 0.362 ±0.021 | 0.543 | 0.179 | 0.268 |
| Logistic bidirectional features | 0.341 ±0.026 | 0.240 | 0.003 | 0.007 |
| GCN (reverse edges) | 0.343 ±0.052 | 0.314 | 0.614 | 0.408 |
| GCN (bidirectional edges) | 0.362 ±0.024 | 0.300 | 0.661 | 0.404 |
| SCP-GNN (reverse only) | 0.360 ±0.023 | 0.369 | 0.568 | **0.446** |
| **SCP-BiD (proposed)** | **0.387 ±0.030** | 0.320 | 0.679 | 0.432 |
| *ablation:* BiD direction-blind | 0.383 ±0.032 | 0.326 | 0.666 | 0.435 |

Paired, 10 seeds:

| vs SCP-BiD | metric | mean diff | 95% CI | p | wins |
|---|---|---|---|---|---|
| SCP-GNN (reverse only) | PR-AUC | **+0.026** | [+0.015, +0.037] | 0.0005 | 9/10 |
| SCP-GNN (reverse only) | F1 | **−0.014** | [−0.021, −0.007] | 0.0014 | 1/10 |
| GCN (bidirectional) | PR-AUC | +0.025 | [+0.007, +0.043] | 0.013 | 8/10 |
| Reachability bidirectional | PR-AUC | +0.242 | [+0.220, +0.263] | <0.001 | 10/10 |
| *ablation:* direction-blind | PR-AUC | +0.003 | [−0.001, +0.008] | **0.15 — ns** | 7/10 |
| *ablation:* direction-blind | F1 | −0.003 | [−0.010, +0.004] | **0.41 — ns** | 5/10 |

### Real incidents (n = 22, 396 bidirectional candidates, 37 positive)

| model | PR-AUC (95% CI) | prec@.5 | rec@.5 | F1@.5 |
|---|---|---|---|---|
| Reachability reverse (SQL today) | 0.365 [0.262, 0.464] | 0.439 | 0.784 | 0.563 |
| Reachability bidirectional | 0.093 [0.073, 0.114] | 0.093 | 1.000 | 0.171 |
| Logistic reverse features | 0.592 [0.441, 0.726] | 0.875 | 0.378 | 0.528 |
| Logistic bidirectional features | 0.591 [0.448, 0.747] | 0.654 | 0.459 | 0.540 |
| GCN (bidirectional edges) | **0.601** [0.486, 0.761] | 0.248 | 0.946 | 0.393 |
| SCP-GNN (reverse only) | 0.416 [0.320, 0.591] | 0.474 | 0.730 | **0.574** |
| SCP-BiD (proposed) | 0.420 [0.332, 0.603] | 0.231 | 0.730 | 0.351 |
| *ablation:* BiD direction-blind | 0.450 [0.346, 0.612] | 0.233 | 0.757 | 0.357 |

### Three findings

**1. The direction flag contributes nothing.** The ablation is statistically
indistinguishable on both metrics (p = 0.15, p = 0.41), and on real data the
direction-blind variant actually scores *higher* (0.450 against 0.420).

The reason is mechanical, and was checked rather than guessed. Mean learned
transmission probability by direction:

| model | reverse | forward | ratio (truth ≈ 0.45) |
|---|---|---|---|
| SCP-BiD, flag on | 0.542 | 0.310 | 0.57 |
| SCP-BiD, direction-blind | 0.525 | 0.329 | **0.63** |

The direction-blind model *still separates the two directions*. Its
transmission function is `MLP([x_src; x_dst; z])`, which is **asymmetric in
its two endpoints by construction**: the same dependency row produces
`[dependency; dependent]` one way and `[dependent; dependency]` the other, and
component type and degree already distinguish those roles. Dir-GNN
(arXiv:2305.10498) needs separate aggregations because GCN-style aggregation
is *symmetric* and genuinely loses direction. An edge-conditioned message
function does not, so for this architecture the explicit flag is redundant.

**So the smallest defensible change is smaller still: just add the reversed
edges. No new feature.**

**2. Forward edges buy ranking and cost precision.** SCP-BiD beats SCP-GNN on
PR-AUC on synthetic (+0.026, p = 0.0005, 9/10) and ties on real data (0.420 vs
0.416, heavily overlapping CIs). But F1 falls significantly on synthetic
(−0.014, p = 0.0014) and badly on real data (0.351 vs 0.574), because
precision halves (0.231 vs 0.474). The candidate set grows 6× and the model
cannot discount the new candidates enough to pay for it.

The model is not failing at its job — it learns forward transmission ≈ 0.31,
correctly, close to the measured 31.8%. It finds 6 of the 8 forward-only
impacts. The arithmetic simply does not work out at a balanced operating point.

**3. There is one regime where it wins, and it is a real one.** Precision at
matched recall on the real incidents:

| model | precision @ recall ≥ 0.95 | precision @ recall = 1.00 |
|---|---|---|
| Reachability reverse (SQL today) | **unattainable** | **unattainable** |
| SCP-GNN (reverse only) | **unattainable** | **unattainable** |
| Reachability bidirectional | 0.093 | 0.093 |
| **SCP-BiD** | **0.224** | **0.159** |

Every reverse-only method is capped at recall 0.784 — 8 of 37 impacts lie
outside its candidate set entirely, so no threshold reaches 0.95. In the
high-recall regime the only alternative to SCP-BiD is bidirectional
reachability, and SCP-BiD is **2.4× more precise at recall ≥ 0.95** and 1.7×
at recall 1.0. Concretely: catch every affected component while paging ~233
components instead of all 396, or catch 95% while paging ~157.

That matters for SEV1 triage, where missing a component is far worse than
paging one unnecessarily. It does not justify replacing SCP-GNN for everyday use.

### Recommendation

- **Keep SCP-GNN as the default.** It is better at a balanced operating point
  on every dataset tested.
- **Do not add the direction flag.** It is redundant with endpoint ordering.
- **Keep SCP-BiD available as an explicit high-recall mode**, not deployed by
  default. It is the only option that can operate above 78.4% recall at all.
- **Next experiment:** incident 22 showed the graph is *missing an edge* that
  the incident narrative names explicitly. Robustness to incomplete dependency
  graphs is now the better target than directionality — it is a different
  failure mode, it is already visible in the real data, and unlike
  directionality it is not blocked by an arithmetic trade-off.
