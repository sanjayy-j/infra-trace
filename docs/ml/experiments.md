# Experiments

Evaluation protocol, results, ablations and limits for SCP-GNN.

**Summary of what the evidence supports.** SCP-GNN significantly beats the
system as it stands (SQL reachability), all three GNN baselines, and both of
its own ablations. It does **not** beat a well-engineered logistic regression
on ranking — the two are statistically indistinguishable (+0.003 PR-AUC,
p = 0.46). Its advantage over that baseline is at the operating point
(F1 +0.172, p < 0.001), not in ranking quality. The real-data evaluation is
22 incidents and settles nothing on its own.

---

## 1. Why training uses synthetic data

The real dataset:

| | count |
|---|---|
| components | 19 |
| dependency edges | 29 |
| incidents with a single root cause | 22 |
| labelled decisions (reachable, non-root) | 66 |
| positives | 29 |

22 scenarios on one 19-node graph cannot train a model and cannot support a
train/test split that means anything. Training therefore runs on generated
estates, and the 22 real incidents are held back entirely as a transfer
check. This follows RiskNet (Rusek et al. 2022, arXiv:2201.12263), which
trains only on generated Barabási–Albert graphs and transfers to real network
topologies.

### The circularity trap

SCP-GNN composes per-edge probabilities with noisy-OR, which is the marginal
form of an Independent Cascade process. **If the generator simulated a pure
IC process, the model would be fitting data drawn from its own hypothesis
class and would win by construction.** Every number below would be an
artefact.

So the ground-truth process in [`ml/synth.py`](../../ml/synth.py) is
deliberately not IC. Each node is assigned one of three mechanisms:

| mechanism | share | behaviour | representable by noisy-OR? |
|---|---|---|---|
| OR | 50% | fails if any incoming dependency transmits | yes |
| **QUORUM** | 25% | fails only when ≥ k dependencies have failed | **no** — a threshold rule is not a function of each edge independently |
| RESILIENT | 25% | retries/circuit breaker damp every incoming probability by a hidden per-node factor | approximately |

Labels then pass through observation noise (5% false negatives, 2% false
positives) because real postmortems miss things.

The proposed model is therefore **misspecified with respect to its own
training data**, which is the honest setting: the flexible GNN baselines have
strictly larger hypothesis classes and are free to do better.
[`test_generator_produces_non_ic_mechanisms`](../../ml/tests/test_ml.py)
fails the build if the generator ever stops producing quorum nodes.

### Generator realism

Synthetic estates are layered the way real ones are — gateway → edge services
→ core services → datastores/external APIs — and dependency types are sampled
conditionally on the endpoint types (a service talking to a queue is
asynchronous; a service reading a database is synchronous or data). Base
transmission rates are *informed* by the rates measured on the real seed data
(see [design.md §1](design.md)) and jittered ±15% per graph so a model cannot
memorise four constants.

The depth profile matches the real graph closely, which matters because depth
is the single strongest predictor:

| depth | synthetic | real |
|---|---|---|
| 1 | 59.6% | 63.6% |
| 2 | 33.4% | 31.8% |
| 3 | 6.6% | 4.5% |
| 4 | 0.5% | — |

---

## 2. Protocol

- **Split by graph, not by cascade.** Every test graph is a topology never
  seen in training. Splitting by cascade would leak: two incidents on one
  graph share all their structure, so a model could memorise the layout and
  be scored on it. 70 train / 20 validation / 40 test graphs, 6 cascades each.
- **Identical inputs for every model.** All neural baselines receive the node
  features, the root indicator, *and* the edge features concatenated into
  each message. Standard GCN ignores edge attributes; withholding them would
  mean a measured gain for SCP-GNN might just be "it saw `dependency_type`
  and the baseline did not".
- **Identical training procedure.** One loop, one optimiser, one early-stopping
  rule for all architectures. Hand-tuning each separately would make the
  comparison uninterpretable.
- **10 seeds.** Each re-draws the data, re-initialises weights and re-trains.
- **Metrics on reachable components only.** Unreachable ones are forced to 0
  by the propagation rule — including them adds free true negatives that
  inflate every metric.
- **PR-AUC is the headline.** Accuracy is useless when most components are
  unaffected by most incidents; "nothing fails" would score well.

Reproduce:

```bash
python -m ml.experiments.run_benchmark --seeds 10      # ~25 min, CPU
python -m ml.experiments.run_real_eval --seeds 5       # needs MySQL
```

---

## 3. Results — synthetic, 40 unseen topologies, 10 seeds

mean ± std across seeds

| model | PR-AUC | precision | recall | F1 | ECE | ECE (calibrated) |
|---|---|---|---|---|---|---|
| Reachability (current SQL) | 0.272 ±0.017 | 0.272 ±0.017 | 1.000 | 0.428 ±0.021 | 0.728 | — |
| Depth decay | 0.345 ±0.017 | 0.000 | 0.000 | 0.000 | 0.027 | — |
| Logistic (path features) | 0.460 ±0.026 | 0.520 ±0.051 | 0.261 ±0.066 | 0.342 ±0.061 | 0.037 | — |
| GCN | 0.404 ±0.035 | 0.360 ±0.046 | 0.578 ±0.193 | 0.426 ±0.043 | 0.219 | 0.050 |
| GraphSAGE | 0.401 ±0.049 | 0.360 ±0.028 | 0.543 ±0.218 | 0.409 ±0.101 | 0.216 | 0.048 |
| GAT | 0.356 ±0.035 | 0.247 ±0.190 | 0.533 ±0.449 | 0.294 ±0.220 | 0.225 | 0.048 |
| **SCP-GNN (proposed)** | **0.464 ±0.027** | 0.408 ±0.038 | 0.706 ±0.054 | **0.514 ±0.025** | 0.185 | 0.049 |
| *ablation:* mean aggregation | 0.433 ±0.027 | 0.496 ±0.066 | 0.162 ±0.027 | 0.242 ±0.028 | 0.069 | 0.043 |
| *ablation:* no edge features | 0.410 ±0.020 | 0.383 ±0.028 | 0.736 ±0.072 | 0.501 ±0.016 | 0.210 | 0.057 |
| *ablation:* no reachability mask | 0.446 ±0.026 | 0.303 ±0.028 | 0.962 ±0.023 | 0.460 ±0.030 | 0.463 | — |

Reachability predicts 1.0 everywhere it reaches, so its recall is 1.0 by
construction and its PR-AUC equals the positive rate — the no-skill line.
Depth decay's fitted λ puts every prediction below 0.5, hence 0 precision and
recall at that threshold; its PR-AUC is the meaningful number.

### Paired significance, 10 seeds

Same data per seed, so differences are paired.

| vs | metric | mean diff | 95% CI | t-test p | wins |
|---|---|---|---|---|---|
| Reachability (current SQL) | PR-AUC | **+0.192** | [+0.171, +0.212] | <0.001 | 10/10 |
| Depth decay | PR-AUC | +0.119 | [+0.101, +0.137] | <0.001 | 10/10 |
| **Logistic (path features)** | **PR-AUC** | **+0.003** | **[−0.007, +0.013]** | **0.46 — not significant** | **5/10** |
| Logistic (path features) | F1 | **+0.172** | [+0.120, +0.224] | <0.001 | 10/10 |
| GCN | PR-AUC | +0.059 | [+0.026, +0.092] | 0.003 | 9/10 |
| GraphSAGE | PR-AUC | +0.062 | [+0.024, +0.101] | 0.005 | 8/10 |
| GAT | PR-AUC | +0.108 | [+0.073, +0.142] | <0.001 | 10/10 |
| *ablation:* mean aggregation | PR-AUC | +0.031 | [+0.014, +0.048] | 0.002 | 9/10 |
| *ablation:* mean aggregation | F1 | +0.273 | [+0.247, +0.299] | <0.001 | 10/10 |
| *ablation:* no edge features | PR-AUC | +0.053 | [+0.042, +0.064] | <0.001 | 10/10 |

### Reading this honestly

**The logistic result is the important one.** A logistic regression on
hand-engineered path features — hop distance, route count, strongest incoming
edge type, node attributes — ranks *as well as* the GNN. The confidence
interval straddles zero and the model wins only 5 of 10 seeds.

So message passing is **not** what produces good ranking on this task. Most
of the ranking signal is in "how far away is it, and what kind of edge
arrives", which a linear model reads directly.

What SCP-GNN does provide over that baseline is a usable operating point.
Logistic regression reaches precision 0.520 at recall 0.261 — it finds about
a quarter of the affected components. SCP-GNN runs at precision 0.408 /
recall 0.706, giving F1 0.514 against 0.342. For paging an on-call engineer,
recall 0.26 is not a usable system regardless of how well it ranks.

### Ablations

| removed | effect |
|---|---|
| **noisy-OR → mean aggregation** | PR-AUC −0.031, but **recall collapses 0.706 → 0.162** and F1 falls 0.514 → 0.242 (p<0.001). Mean aggregation has the wrong monotonicity: a second weak failing dependency *lowers* the mean and so lowers the predicted probability, when physically it should raise it. |
| **edge features** | PR-AUC −0.053 (p<0.001, 10/10). `dependency_type` and `is_critical` carry real signal, consistent with the 40%–86% spread measured on the real data. |
| **reachability-masked loss** | PR-AUC −0.018, but ECE explodes 0.185 → 0.463. Training on structurally-determined zeros teaches the model to predict near-zero everywhere, then `pos_weight` over-corrects. |

Both components of the proposal therefore contribute, and they contribute
*differently*: edge features drive ranking, the composition rule drives the
operating point.

### MLP versus linear link

The one methodological difference between this model and the hyperparametric
IC model of Kalimeris et al. (2018) was using an MLP for the transmission
function instead of their linear `σ(θᵀx)`. Running both under identical
splits and training, 10 seeds:

| transmission function | parameters | PR-AUC | F1 |
|---|---|---|---|
| MLP, 2 hidden layers | 1,441 | 0.4609 ±0.0217 | 0.5129 ±0.0247 |
| **Linear — i.e. hyperparametric IC** | **28** | 0.4593 ±0.0318 | 0.5151 ±0.0290 |

| difference | value | 95% CI | p | wins |
|---|---|---|---|---|
| PR-AUC (MLP − linear) | +0.0016 | [−0.0108, +0.0140] | 0.78 | 4/10 |
| F1 (MLP − linear) | −0.0023 | — | 0.43 | 3/10 |

**The MLP buys nothing.** A 28-parameter linear model matches a
1,441-parameter one — 51× fewer parameters, indistinguishable results.

Two consequences, and they point in opposite directions:

- *Scientifically*, this removes the last methodological difference between
  this model and Kalimeris et al. (2018). SCP-GNN **is** hyperparametric IC.
  [novelty.md](novelty.md) states this plainly.
- *Practically*, it is a good result: the task needs 28 parameters, and
  `θ` in a linear link is directly readable as a per-feature weight, which is
  more interpretable than an MLP. `SCPConfig(linear_link=True)` selects it.
  The MLP is kept as the default only because extra capacity may matter on
  deeper graphs or richer feature sets than this data contains — which is an
  assumption, not something measured here.

### Cost

| | SCP-GNN | GCN |
|---|---|---|
| parameters | **1,441** | 7,329 |
| training (70 graphs, 120 epochs) | ~7 s | ~11 s |
| inference | **0.21 ms/scenario** | 0.24 ms/scenario |

Parameter count is independent of graph size — `p_e` is a function of
features, not of edge identity.

### Calibration

Raw ECE is poor (0.185) because `pos_weight` deliberately inflates predicted
probabilities to buy recall. An isotonic map fitted on **validation** and
applied to test brings it to 0.049 — comparable to every other neural model.

Isotonic regression is non-decreasing, so it never inverts two predictions,
but it does merge distinct scores into ties, and average precision scores
ties differently. PR-AUC therefore moves slightly under calibration; the
benchmark reports `pr_auc` and `pr_auc_calibrated` separately rather than
claiming they are equal.

> Two bugs were caught by assertions while building this, both worth
> recording. sklearn's `IsotonicRegression` defaults to `increasing="auto"`,
> which infers direction from rank correlation and will fit a *decreasing*
> map on a noisy split — silently meaning "more confident ⇒ less likely to
> fail". It is now pinned to `increasing=True`. Separately, the first
> monotonicity test failed because the model was left in training mode, so
> dropout re-sampled the edge probabilities on every forward pass; the
> comparison was between two different models.

---

## 4. Results — real incidents

22 recorded ShopSphere incidents, 66 decisions. Models trained **only** on
synthetic data and never on these. 95% bootstrap CI resamples whole
incidents, not individual components, because components within one incident
are not independent.

| model | PR-AUC (95% CI) | prec@.5 | rec@.5 | F1@.5 | best F1 | @thr |
|---|---|---|---|---|---|---|
| Reachability (current SQL) | 0.439 [0.359, 0.528] | 0.439 | 1.000 | 0.611 | 0.611 | 0.50 |
| Depth decay | 0.601 [0.510, 0.712] | 0.000 | 0.000 | 0.000 | **0.732** | 0.41 |
| Logistic (path features) | **0.771** [0.674, 0.881] | 0.833 | 0.172 | 0.286 | 0.704 | 0.34 |
| GCN | 0.544 [0.439, 0.737] | 0.562 | 0.310 | 0.400 | 0.667 | 0.44 |
| SCP-GNN (proposed) | 0.717 [0.587, 0.857] | 0.786 | 0.379 | 0.512 | 0.675 | 0.29 |

**What this supports.** SCP-GNN ranks substantially better than the deployed
SQL — 0.717 [0.587, 0.857] against 0.439 [0.359, 0.528], intervals that do
not overlap. Synthetic training transfers to a real topology rather than
collapsing.

**What this does not support.** SCP-GNN is not distinguishable from logistic
regression here, and logistic regression has the higher point estimate. On
best-F1, a one-parameter depth heuristic beats everything.

**Why the simple methods do so well on this graph.** It is tiny and shallow —
maximum depth 3, with 95.4% of decisions at depth 1 or 2, and propagation
rates of 61.9% at depth 1 against 9.5% at depth 2. Depth alone nearly
separates the classes. There is very little room for edge-semantic reasoning
to add anything, and only 3 decisions exist at depth 3.

**`best F1` is not a fair comparison.** It selects its threshold *on the test
set*. With 66 decisions that is badly overfit, and it favours models with few
distinct score values — depth decay emits at most three. It is shown for
completeness, not as a ranking.

**The F1@.5 gap is a real deployment problem.** Models trained on synthetic
data whose positive rate is ~26% are being applied to real data whose
positive rate is 44%, so 0.5 is the wrong operating point. A deployment must
pick its threshold on held-out local data.

---

## 5. Limits

1. **n = 22.** Every real-data interval is wide. Nothing here establishes a
   general improvement over simple baselines.
2. **The real data is hand-authored.** `database/data/seed.sql` was written
   for this project. Its propagation patterns are a plausible model of a real
   estate, not a sample from one. The measured 40%–86% transmission spread
   may be an artefact of how the seed was written.
3. **Recall is capped at 78.4%.** 8 of 37 impacts are unreachable by reverse
   traversal. Verified case by case in
   [direction-study.md §1](direction-study.md): 5 are forward propagation
   proper (retry storm, producer pollution, cache churn, consumer stall), 2
   are the same incident attributing *user-visible capability* loss rather
   than component failure, and 1 is unreachable in either direction because
   the dependency graph is **missing an edge** the incident narrative names.
   The earlier description of all 8 as "forward propagation" was too strong.
   No reverse-traversal method can exceed the cap either way.
4. **No temporal modelling.** `started_at`, `resolved_at` and `deployed_at`
   are all available and all unused.
5. **Calibration transfers imperfectly** across the positive-rate shift. An
   adopter should refit the isotonic map on local incident history — the one
   step that genuinely requires local data.
6. **Synthetic realism is an assumption, not a measurement.** The generator's
   parameters are documented in `ml/synth.py` and informed by 29 real edges.

---

## 7. Directionality (SCP-BiD)

A follow-up study asked whether modelling forward propagation — a component
damaging something it depends on — improves blast-radius prediction. Full
protocol, prior art and results in
**[direction-study.md](direction-study.md)**.

Headline: **it fails the pre-registered criterion.** SCP-BiD beats SCP-GNN on
PR-AUC (+0.026, p = 0.0005 on synthetic) and loses on F1 (−0.014, p = 0.0014;
0.351 vs 0.574 on real incidents), because admitting forward edges grows the
candidate set 6× and precision does not survive it. The explicit direction
flag contributes nothing (p = 0.15) because the transmission function is
already asymmetric in its endpoints.

One regime does favour it: at recall ≥ 0.95 — which **no reverse-only method
can reach at all**, being capped at 78.4% — SCP-BiD holds precision 0.224
against bidirectional reachability's 0.093.

SCP-GNN remains the default. SCP-BiD is kept as an experimental high-recall
mode and is not deployed.

## 6. What would move this forward

In rough order of expected value:

1. ~~**Model both edge directions.**~~ **Done, and it does not pay off at a
   balanced operating point.** See [direction-study.md](direction-study.md)
   and §7 below. Forward edges lift the recall ceiling to 97.3% as predicted,
   but inflate the candidate set 6× and halve precision; SCP-BiD beats
   SCP-GNN on PR-AUC and loses on F1. It wins only in the high-recall regime.
   The explicit direction flag turned out to be redundant.
2. **Get real multi-organisation incident data.** Everything else is bounded
   by having one small graph. Public microservice traces (e.g. the Alibaba
   cluster traces) could supply topology at realistic scale.
3. **Use the timestamps.** Predicting propagation *order* is a strictly harder
   and more useful target than the terminal set, and the columns are there.
4. **Test on deeper graphs.** The advantage of composition over hand-crafted
   depth features should grow with path length; the current data cannot show
   this because real paths are 1–3 hops.
5. **Relax the independence assumption** to cover quorum nodes directly —
   the known failure mode in [design.md §5](design.md).
