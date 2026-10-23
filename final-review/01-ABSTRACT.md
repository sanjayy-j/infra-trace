# Abstract and contribution statement

## Abstract

Service-dependency impact analysis — "if this component fails, what else
breaks?" — is normally answered by graph reachability. InfraTrace implements
that baseline directly in SQL: a recursive CTE over a normalised MySQL schema
returns every component with a dependency path to the failure. Reachability is
sound but indiscriminate: it treats a non-critical asynchronous dependency
exactly like a critical synchronous one. Measured against 22 recorded
incidents, it flags 66 components where 37 were affected — precision 43.9%.

We add a learned probabilistic layer over the same database. Each dependency
edge is scored from its recorded semantics (`dependency_type`, `is_critical`)
and its endpoints' attributes; the per-edge transmission probabilities are then
composed along paths by a noisy-OR fixed point, so the model's output is a
failure probability rather than a reachability flag, and its intermediate
quantities are probabilities on real dependency edges.

We evaluate on a **real production dependency graph** — 708 services and 1,529
edges from ChronoGraph (Bitdefender) — using 30 hub-stratified, service-disjoint
partitions. Cascades over that topology are simulated, so this isolates the
effect of real structure, and the results are not validated predictions of
actual production failures.

On held-out partitions the model reaches **PR-AUC 0.5519 ±0.045**. The
comparison that matters is against the practice it would replace. Deterministic
SQL reachability reaches 0.3216. A hand-authored dependency-multiplier scheme of
the kind operational tooling documents reaches **0.5099** under its own
max-product rule, and SCP-GNN improves on it by **+0.0420, winning 29 of 30
partitions**. The strongest baseline, however, is neither of those and is not
logistic regression (0.5067, with GraphSAGE 0.4724 and GCN 0.4691): it is the
**same heuristic with its eight weights fitted on training data**, which reaches
**0.5379**. Against that, SCP-GNN's improvement is **+0.0141, winning 27 of 30
partitions** — and it is *worse* calibrated (ECE 0.1316 vs 0.1204) and slightly
less precise (0.4977 vs 0.5073) than the fitted heuristic it narrowly outranks.
Against logistic regression the gain is concentrated in recall (0.683 vs 0.292)
and likewise paid for in precision (0.498 vs 0.569) and calibration
(ECE 0.132 vs 0.038).

We report four negative results — bidirectional propagation, missing-edge
recovery, isotonic calibration and class-weight removal — each rejected against
pre-registered criteria, and we document a measured 78.4% ceiling on recall
imposed by dependency direction that no reverse-traversal method can exceed.

## Contribution statement

**What this work contributes:**

1. **A database-backed probabilistic impact layer.** The SQL reachability
   result is preserved unchanged and the model annotates it; the API reports
   structure and estimate as separate fields. The model provably cannot flag a
   component the recursive CTE did not already find.

2. **A controlled comparison of learned propagation against the practice it
   would replace** — deterministic reachability, hand-authored dependency
   weights, and a compact fitted heuristic — on identical partitions, labels and
   metric code, with the propagation rule held fixed so that only the source of
   the per-edge probability varies. Two thirds of the improvement available over
   the hand-authored scheme comes from correcting eight numbers; the remaining
   third, delivered by the full model, is paid for in calibration and precision.
   This quantifies the value *and the limits* of learned propagation in the
   evaluated setting.

3. **An evaluation on real production topology.** We show the project's own
   earlier conclusion — that logistic path features match the propagation model
   — was an artifact of degree-homogeneous synthetic graphs (in-degree Gini
   0.513 vs the real 0.781, max in-degree 4 vs 73). On a corrected substrate
   built from a real 708-service graph with hub-stratified partitions, the
   propagation model leads consistently.

4. **A measured structural ceiling.** 8 of 37 recorded impacts travel *forwards*
   along a dependency — a caller damaging what it calls — capping recall at
   78.4% for the deployed SQL and for every model built on reverse traversal.

5. **Four pre-registered negative results**, each with a diagnosed cause,
   including the finding that calibration and the operating point trade against
   each other in both directions.

**What this work does not contribute.** No algorithmic novelty is claimed. The
model is the hyperparametric Independent Cascade formulation (Kalimeris et al.,
2018), applied to cascading failures by Xiang et al. (2024). The topology
dependence we observe is consistent with Fountoulakis et al. (2023). This is an
**applied, system-level and empirical** contribution — the database integration,
the controlled comparison, the corrected evaluation and the negative results —
not a new propagation algorithm.

Three limits are part of the claim, not footnotes to it. The corrected topology
study **failed two of its six pre-registered criteria** (minimum partition gap
−0.0122 against a >−0.01 rule; ECE regression +0.0938 against ≤0.02), and is
reported as such. All 30 partitions re-partition one organisation's graph, so
nothing here establishes independent-system generalisation, and no significance
test was used as a decision criterion. The cascades are simulated, so no result
is a validated prediction of an observed production failure.
