# Results

## Protocol

**30 service-disjoint, hub-stratified partitions of one real 708-service
production dependency graph.** 3 initialisations averaged within partition; the
partition is the experimental unit. **TEST-R** (uniform-seeded subgraphs) is the
primary evaluation set; **TEST-H** (hub-seeded) is a secondary diagnostic and is
never pooled with it. Cascades are **simulated** over the real topology.

Substrate: pools ≈ 352/144/212 services; TEST-R covers 147/212 services across
32 subgraphs per partition; test pools reach **global in-degree 73** with mean
hub retention 65%. Runtime 176.5 min.

## Main table — TEST-R, mean over 30 partitions

| model | PR-AUC | precision | recall | F1 | ECE |
|---|---|---|---|---|---|
| Reachability (SQL, deployed) | 0.3216 ±0.050 | 0.3216 | **1.0000** | 0.4845 ±0.060 | 0.6784 |
| Depth decay | 0.3968 ±0.025 | 0.0000 | 0.0000 | 0.0000 | 0.0398 |
| Logistic (path features) | 0.5067 ±0.038 | **0.5685** | 0.2920 | 0.3771 ±0.085 | **0.0378** |
| GCN | 0.4691 ±0.071 | 0.4050 | 0.6373 | 0.4732 ±0.078 | 0.1774 |
| GraphSAGE | 0.4724 ±0.074 | 0.3909 | 0.6389 | 0.4745 ±0.102 | 0.1734 |
| GAT | 0.3910 ±0.065 | 0.2291 | 0.5498 | 0.3101 ±0.217 | 0.1766 |
| **SCP-GNN** | **0.5519 ±0.045** | 0.4977 | 0.6826 | **0.5696 ±0.035** | 0.1316 |

Reachability predicts 1.0 wherever it reaches, so its recall is 1.0 by
construction and its PR-AUC equals the positive rate — the no-skill line. Depth
decay's fitted λ puts every prediction below 0.5, hence zero precision and
recall at that threshold; its PR-AUC is the meaningful figure.

## Partition-level consistency

SCP-GNN minus each baseline, PR-AUC, paired within partition:

| vs | mean | median | partitions won |
|---|---|---|---|
| Reachability (SQL) | +0.2303 | +0.2288 | **30/30** |
| GAT | +0.1609 | +0.1444 | **30/30** |
| Depth decay | +0.1551 | +0.1557 | **30/30** |
| GCN | +0.0828 | +0.0713 | 29/30 |
| GraphSAGE | +0.0795 | +0.0700 | 29/30 |
| Logistic (path features) | +0.0452 | +0.0480 | 29/30 |

TEST-H (secondary) agrees: +0.0431 vs logistic, 29/30.

**This is partition-level consistency on one graph. It is not
independent-system generalisation** — all 30 partitions re-partition the same
708 services from the same organisation, so partition values are correlated and
no confidence interval here has valid coverage for a population of graphs. No
significance test was used as a decision criterion.

## A stronger baseline, measured after this table was frozen

A fixed-weight propagation heuristic — IncidentRelay's documented multiplier
scheme, and an **eight-cell** version of it fitted on training data — was run
afterwards on this exact TEST-R data, with the regenerated labels verified
against the frozen baselines to 1e-12.

| model | PR-AUC | SCP-GNN gap | partitions won |
|---|---|---|---|
| Hand-authored, max-product rule (the documented incumbent) | 0.5099 | **+0.0420** | **29/30** |
| Hand-authored, noisy-OR rule | 0.5216 | +0.0303 | 30/30 |
| **Fitted eight-cell heuristic — the strongest baseline** | **0.5379** | **+0.0141** | **27/30** |

SCP-GNN beats the documented incumbent by +0.0420, which is the point of
running it. But the fitted eight-cell heuristic reaches 0.5379, ahead of every
baseline in the table above, so **SCP-GNN's lead over the strongest baseline is
+0.0141, not +0.0452** — and that heuristic is **better calibrated**
(ECE 0.1204 vs 0.1316, better in 19/30) and **slightly more precise**
(0.5073 vs 0.4977, better in 18/30) than SCP-GNN. Full result and caveats:
[08-FIXED-WEIGHT-BASELINE.md](08-FIXED-WEIGHT-BASELINE.md).

## The trade-off

SCP-GNN's advantage is **not uniform across metrics**:

| metric | SCP-GNN | logistic | SCP better in |
|---|---|---|---|
| recall | 0.6826 | 0.2920 | 30/30 |
| F1 | 0.5696 | 0.3771 | 30/30 |
| **precision** | 0.4977 | 0.5685 | **3/30** |
| **ECE** | 0.1316 | 0.0378 | **2/30** |

SCP-GNN finds more than twice as many affected components, and pays for it in
precision and calibration. For triage — where missing a component is worse than
paging one unnecessarily — that is the right trade. For a system quoting
probabilities to an operator, the calibration gap matters, and we investigated
it rather than ignoring it.

## Calibration, investigated and not resolved

Two interventions were pre-registered and both failed their guards:

| intervention | ECE | PR-AUC | F1 | verdict |
|---|---|---|---|---|
| baseline | 0.1316 | 0.5519 | 0.5696 | — |
| isotonic (validation-fitted) | **0.0430** | 0.5354 | 0.4330 | F1 −0.137 |
| remove `pos_weight` (w=1.0) | **0.0387** | 0.5535 | 0.4510 | F1 −0.119, guard failed in 30/30 |

The miscalibration is **monotone overconfidence** consistent with `pos_weight`
inflating probabilities: weighted BCE with weight *w* has minimiser
`p* = wq/(wq + 1−q)`, which at the measured w = 2.180 predicts the observed
reliability curve with mean absolute error 0.043 and correlation 0.994.

Both routes to good calibration cost roughly 0.12–0.14 F1. **The default was
not changed**, because the pre-registered F1 guard failed. Calibration and the
operating point trade against each other here in both directions; we report
that rather than selecting whichever configuration looks best.

## Pre-registered criteria: not met

| criterion | value | result |
|---|---|---|
| median gap ≥ +0.02 | +0.0480 | PASS |
| ≥ 26/30 partitions positive | 29/30 | PASS |
| min partition gap > −0.01 | **−0.0122** | **FAIL** |
| gap in >70% hub-retention stratum | +0.0605 | PASS |
| F1 regression ≤ 0.02 | +0.1926 | PASS |
| **ECE regression ≤ 0.02** | **+0.0938** | **FAIL** |

**The study did not pass its own acceptance rule**, on two of six checks. We
report it as such and did not weaken the thresholds after seeing results.

## Negative results

| direction | outcome |
|---|---|
| **SCP-BiD** (bidirectional propagation) | +0.026 PR-AUC synthetic, but F1 −0.014 (p=0.0014); on real data indistinguishable. Rejected. Wins only above 95% recall, where no reverse-only method can operate at all. |
| **SCP-Latent** (missing-edge recovery) | Worse in **all six** removal configurations; `top_k=0` beat `top_k=2`. Link prediction PR-AUC 0.079 against a 2.1% base rate — candidates grow O(N²) while missing edges grow O(N). |
| **Isotonic calibration** | ECE −0.089 but F1 −0.137. Guard failed. |
| **`pos_weight` removal** | ECE −0.093, PR-AUC +0.002, F1 −0.119 in 30/30. Guard failed. |

## The structural ceiling

**8 of 37** recorded impacts in the real incident data are unreachable from
their root cause by reverse traversal — a caller damaging what it calls
(a rate limiter rejecting valid traffic; a retry storm hitting a database).
Recall is therefore capped at **78.4%** for the deployed SQL, for SCP-GNN, and
for any other method built on the same traversal. Lifting it requires modelling
the forward direction, which we tried (SCP-BiD) and which cost more precision
than the recall was worth.

## Substrate correction

The evaluation substrate itself was a finding. Against the real graph, the
original synthetic generator was unrepresentative in measurable ways:

| property | real (708 services) | original synthetic |
|---|---|---|
| in-degree Gini | **0.781** | 0.513 |
| max in-degree | **73** | 4 |
| acyclic | no (22 cycles) | 95% of graphs |
| max depth | 3 | 2.5 |
| zero-blast components | 43% | 42% |

Depth and the zero-blast fraction transferred; degree concentration and
acyclicity did not. An earlier conclusion of this project — that logistic path
features matched the propagation model — **did not survive** the corrected
substrate.
