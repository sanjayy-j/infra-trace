# Claims status — read this before presenting anything

Every claim in the submission is classified here. The classification is the
governing document: if another file appears to say more than this one permits,
this one wins.

---

## A. VERIFIED — reproducible from stored artifacts

| # | claim | evidence |
|---|---|---|
| A1 | On 30 held-out partitions of a real 708-service production dependency graph, SCP-GNN reaches **PR-AUC 0.5519 ±0.045**, ahead of logistic path features (0.5067), GraphSAGE (0.4724), GCN (0.4691), GAT (0.3910), depth decay (0.3968) and SQL reachability (0.3216). | `topology_v2.json`, 30 partitions × 3 inits |
| A2 | The advantage over logistic is **consistent at partition level**: +0.0452 mean, positive in **29/30** partitions; over GCN +0.0828 (29/30); over reachability +0.2303 (30/30). | same |
| A3 | SCP-GNN wins on **recall** (0.683 vs 0.292) and **F1** (0.570 vs 0.377, 30/30) and **loses on precision** (0.498 vs 0.569, better in only 3/30) and **calibration** (ECE 0.132 vs 0.038, better in 2/30). | same |
| A4 | Removing `pos_weight` cuts raw ECE **0.1316 → 0.0387** and slightly raises PR-AUC (0.5519 → 0.5535) but costs F1 **0.5696 → 0.4510** (worse in 30/30). The pre-registered F1 guard failed; the default was not changed. | `posweight_ablation.json` |
| A5 | Validation-fitted isotonic calibration cuts ECE **0.1316 → 0.0430** but costs PR-AUC −0.0166 and F1 −0.1366. | `calibration_diagnostic.json` |
| A6 | **8 of 37** recorded impacts in the real incident data are unreachable from their root cause by reverse traversal, capping recall at **78.4%** for the SQL and for any model built on that traversal. | `docs/ml/direction-study.md`, verified against MySQL |
| A7 | The dependency graph of 708 production services has in-degree Gini **0.781** and max in-degree **73**, against 0.513 and 4 for the original synthetic generator. The original evaluation substrate was unrepresentative in a measured way. | `docs/ml/topology-study.md`, ChronoGraph `edges.csv` |
| A8 | The frozen acceptance criteria for the topology study were **not met** (min partition gap −0.0122 vs rule >−0.01; ECE regression +0.0938 vs rule ≤0.02). | `topology_v2.json` |
| A9 | Under the corrected protocol the test pool reaches **global in-degree 73** (mean hub retention 65%), versus max 10 under the earlier fixed split. | `topology_v2.json` |
| A10 | Against a **fixed-weight propagation heuristic** of the kind IncidentRelay documents, SCP-GNN is ahead by **+0.0420 PR-AUC in 29/30** partitions (its own documented max-product rule) and **+0.0303 in 30/30** (noisy-OR rule). Evaluated on the identical TEST-R data, verified to 1e-12 against the frozen baselines. | `fixed_weight_baseline.json` |
| A11 | A **fitted eight-cell** version of that heuristic (9 cells, 8 of them exercised by this data), trained on train/val only, reaches **PR-AUC 0.5379** — ahead of logistic path features (0.5067), GCN, GraphSAGE and GAT — and is **better calibrated** (ECE 0.1204 vs 0.1316) and **slightly more precise** (0.5073 vs 0.4977) than SCP-GNN, which leads it by only **+0.0141 PR-AUC (27/30)**. | same |

> **Amendment (2026-10-16).** A2 and A3 were written when logistic path
> features were the strongest baseline. They are not: A11's fitted eight-cell
> heuristic is. The A2/A3 figures are unchanged and correct *as stated against
> logistic*, but **"wins against every baseline" is no longer accurate** and
> must not be said — and logistic regression must not be presented as the
> strongest baseline. The honest pair of margins is **+0.0420 (29/30)** over
> the documented hand-authored heuristic and **+0.0141 (27/30)** over the
> fitted one. See [08-FIXED-WEIGHT-BASELINE.md](08-FIXED-WEIGHT-BASELINE.md).
> No frozen artifact, registered criterion, evaluation protocol or model was
> altered; A8's failed criteria stand unchanged.

## B. PRELIMINARY — must be labelled as such

| # | claim | why it is only preliminary |
|---|---|---|
| B1 | A **28-parameter linear** transmission function matches the 1,441-parameter MLP (reported: real PR-AUC 0.635 vs 0.629; real best-F1 +0.0015, p=0.78; 15 seeds). | **No saved artifact and no script exist.** It was run inline, so it is not reproducible from the repository and has no stored per-seed values. It also used the **older substrate** (synthetic-topology training, 22 real incidents), *not* the corrected 30-partition protocol. Two independent reasons. |
| B2 | The learned linear coefficients reproduce the measured transmission ordering (Synchronous+critical ≫ Synchronous > Data ≫ Asynchronous). | Same provenance problem as B1. The ordering is checked by a passing test, but the quantitative comparison is not stored. |
| B3 | A route-count calibration gradient exists (gap rises with number of incoming routes). | Survived the quorum control (−13%, not the ≥50% that would indicate an artifact) but is positive in only **20/27** partitions and ranges −0.098 to +0.224. Exploratory. |

**If asked about B1 at review: say it is a promising preliminary result that has not been re-run under the final protocol.** Do not present it as a headline.

## C. MUST NOT BE CLAIMED

| # | forbidden claim | why |
|---|---|---|
| C1 | **Algorithmic novelty of SCP-GNN.** | It is the hyperparametric Independent Cascade model (Kalimeris et al. 2018), applied to cascading failures by Xiang et al. 2024. Documented in `docs/ml/novelty.md`. |
| C2 | **Generalisation to other systems.** | All 30 partitions re-partition **one** 708-service graph from **one** organisation. There is no independent replication. |
| C3 | **Statistical significance of the partition comparisons.** | Partitions share services, so they are not independent; no interval here has valid coverage. Decisions were made on effect size and sign consistency, not p-values. |
| C4 | **Real-world failure prediction performance.** | Cascades on the production topology are **simulated**. ChronoGraph publishes no propagation labels. The only real propagation evidence is 22 incidents on a 19-node graph. |
| C5 | That the topology study **passed** its criteria. | It did not (A8). |
| C6 | That removing `pos_weight` or applying isotonic calibration is a contribution. | Both are textbook; both failed their guards (A4, A5). |
| C7 | That SCP-BiD, incomplete-graph recovery, or RRD produced an improvement. | All rejected with diagnosed causes. |
| C8 | Any claim about components the model never scores. | The evaluation mask excludes unreachable components (0 of 124,412 decisions), and the simulator cannot label one positive. |

---

## The contribution statement, as it may be made

> InfraTrace combines database-backed service dependency management with
> deterministic blast-radius analysis and probabilistic impact ranking. Its
> empirical contribution is a controlled comparison of learned propagation
> against deterministic reachability, hand-authored dependency weights, and a
> compact fitted heuristic. On 30 partitions of a real production dependency
> topology with simulated cascades, SCP-GNN improves PR-AUC by 0.0420 over the
> documented hand-authored heuristic and by 0.0141 over the fitted heuristic,
> while exhibiting precision and calibration trade-offs. These findings
> quantify the value and limitations of learned propagation in the evaluated
> setting; they do not establish a novel propagation algorithm or
> generalisation to observed production failures.

This is an **applied, system-level and empirical** contribution. C1–C8 below
remain in force: in particular the study still failed two of its six
pre-registered criteria (A8), and nothing here is a validated prediction of an
observed production failure (C4).

## The one-sentence honest summary

> On a real production dependency graph, a learned probabilistic propagation
> model consistently outranks SQL reachability and several standard baselines
> for service-impact prediction, at a measured cost in precision and
> calibration — established on simulated cascades over one organisation's
> topology, with no claim of algorithmic novelty.
