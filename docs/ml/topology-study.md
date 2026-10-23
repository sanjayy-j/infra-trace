# Topology realism study

Phase 1–4 of the "find one novel, measurably better method" brief. Results in §5.

**Verdict up front.** No novel algorithm is claimed, and none was found. What
was found is that the *evaluation substrate* underlying every earlier
conclusion is unrealistic in specific, measured ways, and that correcting it
changes a published conclusion. That is an empirical contribution, not an
algorithmic one, and §6 says exactly how far it can be defended.

---

## 1. Ranked research-gap analysis

Five distinct directions were investigated. Each was gated on a cheap premise
check, because a direction whose premise fails cannot be rescued by
implementation.

### 1. Topology realism — SELECTED

| | |
|---|---|
| **Limitation** | Every result in `docs/ml/` was measured on `ml/synth.py` graphs. Their realism was never checked. Against ChronoGraph's real 708-service graph: in-degree Gini **0.51 vs 0.78**, max in-degree **4 vs 73**, acyclic **95% vs 0%**. |
| **Closest prior work** | Fountoulakis et al. 2023, [arXiv:2310.07667](https://arxiv.org/abs/2310.07667): maps GNN performance against random-graph structure, finding **heavy-tailed degree distributions enhance GNN performance** and that GCNs provably cannot beat linear models under some conditions. |
| **Technical difference** | None in mechanism. The contribution is applying that result to decide whether InfraTrace's "logistic ties the GNN" conclusion is a substrate artefact. |
| **Data** | ChronoGraph `edges.csv`, 708 nodes / 1529 edges, Apache-2.0, 33 KB. Already obtained. |
| **Cost** | Low — a sampler, no new model. |
| **Falsifiable hypothesis** | The SCP-GNN − logistic gap is larger on real topology than on synthetic. If the gap is unchanged, the published conclusion stands and this direction is dead. |

### 2. Graded impact severity instead of binary failure

| | |
|---|---|
| **Limitation** | Everything so far predicts a binary "affected / not". `incident_component.impact_level` records four grades. |
| **Closest prior work** | Ordinal regression / cost-sensitive classification; standard. |
| **Premise check** | 37 non-root impacts: Degraded 24, Minor 9, **Unavailable 4**. |
| **Verdict** | **Eliminated.** Four examples of the most severe class cannot support a three-class evaluation at any confidence. |

### 3. Deployment-risk prediction

| | |
|---|---|
| **Limitation** | `deployment` and `incident.caused_by_deployment_id` are recorded and entirely unused. Predicting "will this deploy cause an incident" is operationally valuable and a genuinely different task. |
| **Closest prior work** | Just-in-time defect prediction (Kamei et al.); change-risk models. Mature field. |
| **Premise check** | 46 deployments, **12** causing an incident. |
| **Verdict** | **Eliminated.** 46 examples, and the task is well-served by an established literature this project would not advance. |

### 4. Temporal / causal propagation order

| | |
|---|---|
| **Limitation** | Predicting *which* components fail ignores *when*. Order is strictly more informative. |
| **Closest prior work** | Temporal graph learning; ErrorPrism ([arXiv:2509.26463](https://arxiv.org/abs/2509.26463)) reconstructs propagation paths post-hoc. |
| **Premise check** | InfraTrace records `started_at`/`resolved_at` per **incident**, not per affected component. There is no per-component timestamp, so propagation order is not recoverable. |
| **Verdict** | **Eliminated — the label does not exist.** |

### 5. Root-cause localisation (the inverse problem)

| | |
|---|---|
| **Limitation** | InfraTrace predicts forwards. Localisation (symptoms → cause) has far more data available. |
| **Closest prior work** | OpenRCA (ICLR'25), MicroHECL, RCAEval, and Fang et al. 2025 ([arXiv:2510.04711](https://arxiv.org/abs/2510.04711)). |
| **Premise check** | Real data exists at scale — OpenRCA has 335 failures and 68 GB of telemetry. But it is a **different task** with an active SOTA field, and 68 GB is not tractable here. |
| **Verdict** | **Eliminated on scope and feasibility**, not on merit. |

### 6. Cycle-aware propagation (noted, not selected)

The real graph has 22 short cycles; InfraTrace's schema *forbids* cycles by
trigger, and `ml/synth.py` is 95% acyclic. So the production reality cannot be
represented in the schema at all. Interesting, but it is a schema finding
rather than an algorithmic one, and the cycles are few.

---

## 2. Data reassessment

| dataset | what it has | licence | verdict |
|---|---|---|---|
| **InfraTrace** | 19 components, 29 edges, 22 incidents with real propagation labels | own | The **only** real propagation evidence. Too small for generalisation. |
| **ChronoGraph** ([arXiv:2509.04449](https://arxiv.org/abs/2509.04449)) | 708 services, 1529 dependency edges, metric time series, expert incident windows | **Apache-2.0** | **Used — topology only.** `edges.csv` is 33 KB and obtained. The features and labels are a **1.79 GB** Git-LFS object and are node-level anomaly windows, not propagation scenarios. |
| **OpenRCA** (ICLR'25) | 335 failures, 68 GB telemetry, 3 enterprise systems | MIT | Rejected: different task (localisation), impractical size, no dependency-propagation labels. |
| **RCAEval** | 46 nodes, 28 root-cause categories | open | Rejected: localisation benchmark. |
| **Fang et al. 2025 benchmark** | 1430 validated failures, hierarchical labels | — | Noted. Its finding that *"simple rule-based methods can achieve performance comparable to or surpassing SOTA models"* on existing RCA benchmarks independently corroborates this project's repeated logistic-ties-GNN result. |

**The separation this study maintains:** ChronoGraph supplies real *structure*.
It supplies no propagation labels, so cascades over it are still simulated by
`ml/synth.simulate_cascade`. Everything in §5 is therefore **a synthetic
process on real structure** — an isolation of the topology variable, not
real-world validation. The 22 InfraTrace incidents remain the only real
propagation evidence in this project.

---

## 3. The substrate gap, measured

ChronoGraph's full graph against what `ml/synth.py` produces:

| property | REAL (708 services) | `ml/synth.py` | real subgraphs (this study) |
|---|---|---|---|
| in-degree Gini | **0.781** | 0.513 | **0.766** (test pool) |
| max in-degree | **73** | 4 | — |
| max out-degree | **103** | 5 | — |
| edges / node | 2.16 | 1.38 | 1.82 |
| zero-blast components | 43% | 32% | 41% |
| max depth | **3** | 2.5 | 2.5 |
| acyclic | **no** (22 cycles) | 95% of graphs | inherits real cycles |

Two things the generator gets **right**, and they matter because earlier
conclusions rest on them:

- **Depth 3 in a real 708-service system.** `docs/ml/experiments.md` explained
  the strength of the depth-decay baseline by the graph being only 3 hops
  deep, and flagged it as possibly an artefact of a 19-node toy. It is not —
  real production topology is just as shallow. That conclusion **generalises**.
- **43% of components have nothing depending on them**, against 42% synthetic.

Two things it gets **badly wrong**: degree concentration and acyclicity.

---

## 4. Method and experiment

No new model. The experiment re-runs the existing comparison on real
structure, holding everything else fixed.

**Substrates.** (a) `ml/synth.py` graphs, as published. (b) Connected subgraphs
of ChronoGraph, 20–40 services.

**Held constant:** models, training loop, features, cascade process, metrics,
seeds. Only topology differs, so a change in the gap is attributable to
topology alone.

**Leakage control.** The 708 services are partitioned once into BFS-contiguous
train/val/test pools (`split_node_pools`); subgraphs are grown within a pool
and never straddle one. A random node partition was tried first and **shattered
connectivity** — the validation pool produced no usable topology at all — so
pools are contiguous regions. The cost is that pools are adjacent regions of
one system rather than independent samples; see §6.

**Attributes are synthetic.** ChronoGraph publishes no component types or
criticality. Types are inferred from structural role (nothing depends on it →
gateway-like; it depends on nothing → datastore-like); criticality is sampled
from InfraTrace's marginal **independently of degree**, deliberately, so the
feature cannot manufacture a signal that predicts blast radius.

**Reproduce:**

```bash
curl -sL -o ml/data/chronograph/edges.csv \
  https://raw.githubusercontent.com/bit-ml/ChronoGraph/main/dataset/edges.csv
python -m ml.experiments.run_topology --seeds 10
python -m pytest ml/tests -q
```

---

## 5. Results

**H1 is confirmed on PR-AUC and rejected on F1.** Both are reported.

### Substrate shape (test split)

| | topologies | nodes | in-degree Gini | edges/node | positive rate |
|---|---|---|---|---|---|
| REAL full graph | 1 | 708 | 0.781 | 2.16 | — |
| SYNTHETIC | 32 | 18 | 0.525 | 1.32 | 0.287 |
| **REAL subgraphs** | 32 | 28 | **0.760** | 1.33 | 0.231 |

### Model comparison, 10 seeds

| model | PR-AUC synthetic | PR-AUC **real** | change |
|---|---|---|---|
| Reachability (SQL) | 0.268 ±0.019 | 0.250 ±0.014 | −0.018 |
| Depth decay | 0.342 ±0.021 | 0.374 ±0.013 | +0.032 |
| Logistic (path feats) | 0.458 ±0.034 | 0.455 ±0.022 | −0.003 |
| GCN | 0.398 ±0.038 | 0.340 ±0.033 | **−0.058** |
| GraphSAGE | 0.406 ±0.049 | 0.337 ±0.024 | **−0.069** |
| GAT | 0.359 ±0.045 | 0.275 ±0.026 | **−0.084** |
| **SCP-GNN** | 0.459 ±0.033 | **0.488 ±0.025** | **+0.029** |

**SCP-GNN is the only model that improves on realistic structure.** All three
conventional GNNs degrade, GAT severely (F1 0.390 → 0.160). Compositional
noisy-OR propagation holds up on hub-dominated graphs where learned-embedding
message passing does not — consistent with Fountoulakis et al.'s finding that
structure governs when message passing pays.

### The hypothesis test

| substrate | metric | gap (SCP-GNN − logistic) | 95% CI | p | wins |
|---|---|---|---|---|---|
| synthetic | PR-AUC | +0.0008 | [−0.024, +0.025] | 0.945 ns | 4/10 |
| **real** | **PR-AUC** | **+0.0335** | **[+0.021, +0.047]** | **0.0003 \*\*\*** | **10/10** |
| synthetic | F1 | +0.1395 | [+0.068, +0.211] | 0.0017 ** | 10/10 |
| real | F1 | +0.0943 | [+0.037, +0.152] | 0.0047 ** | 10/10 |

Difference of gaps: **PR-AUC +0.0328, Welch p = 0.018**; F1 −0.045, p = 0.28.

So the *ranking* tie reported in [experiments.md](experiments.md) — +0.003,
p = 0.46 — **reproduces exactly on synthetic topology (+0.0008, p = 0.945) and
disappears on real topology (+0.0335, p = 0.0003, 10/10 seeds)**. The F1
advantage, by contrast, was always present and does not grow; H1 is not
supported there, and claiming it would be reading one metric selectively.

### Ruling out the obvious confound

Real subgraphs are larger (28 vs 18 nodes) and have a different positive rate
(0.231 vs 0.287). A control substrate was generated: synthetic graphs enlarged
to match the node count and base rate while keeping the degree distribution
homogeneous.

| substrate | nodes | in-Gini | positive rate | gap | p | wins |
|---|---|---|---|---|---|---|
| small synthetic | 18 | 0.525 | 0.287 | +0.0008 | 0.945 | 4/10 |
| **control: large synthetic** | **30** | **0.554** | **0.261** | **+0.0044** | **0.578** | **6/10** |
| **REAL** | 28 | **0.760** | 0.231 | **+0.0335** | **0.0003** | **10/10** |

The control matches the real substrate on size and base rate and differs only
in degree concentration. **The gap stays null.** Size and class balance do not
explain it; degree concentration does.

---

## 5b. Verdict

**Novelty: no.** The mechanism is established — Fountoulakis et al. (2023)
already show heavy-tailed degree distributions enhance GNN performance and
that GCNs cannot beat linear models under some conditions. Nothing here is a
new algorithm, and SCP-GNN itself remains hyperparametric Independent Cascade
([novelty.md](novelty.md)).

**Measurable improvement: yes, and controlled.** On real production topology
SCP-GNN beats logistic regression on path features by +0.0335 PR-AUC
(p = 0.0003, 10/10 seeds) and beats every conventional GNN baseline by
0.15–0.21. On the synthetic substrate it beat none of them on ranking. The
difference is attributable to degree concentration, with size and base rate
controlled.

**What the contribution actually is:** evidence that a published conclusion of
this project was an artefact of its evaluation substrate, and a corrected
substrate built from a real production graph. That is an empirical and
methodological contribution, not an algorithmic one.

---

## 6. Limits of what this can show

- **Not real-world validation.** Real structure, simulated process. No
  propagation labels exist in ChronoGraph.
- **One organisation.** 708 services from one company. "Real topology" here
  means Bitdefender's, not microservice topology in general.
- **Pools are adjacent regions**, not independent systems, because contiguity
  was required to keep subgraphs connected. Train and test share no service,
  but they are neighbourhoods of one graph.
- **Component attributes are invented.** Only the wiring is real.
- **Subgraphs cannot contain the largest hubs.** A 20–40 service subgraph
  cannot hold a degree-73 node, so the test substrate reaches Gini 0.77 rather
  than the full graph's 0.78 — close, but the extreme tail is absent.
