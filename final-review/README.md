# Final review materials

Prepared from existing verified artifacts. **No experiment was re-run, no
frozen result modified, nothing committed.**

| file | purpose |
|---|---|
| [00-CLAIMS-STATUS.md](00-CLAIMS-STATUS.md) | **Read first.** What is verified, what is preliminary, what must not be claimed. Governs every other file. |
| [01-ABSTRACT.md](01-ABSTRACT.md) | Abstract and contribution statement |
| [02-METHODS.md](02-METHODS.md) | Graph construction, labels, simulation, baselines, metrics, partition protocol, leakage controls |
| [03-RESULTS.md](03-RESULTS.md) | Results table, partition consistency, trade-offs, negative results |
| [04-LIMITATIONS.md](04-LIMITATIONS.md) | Ten limitations, stated plainly |
| [05-DEMO-SCRIPT.md](05-DEMO-SCRIPT.md) | 5-minute demo, verified commands and expected output |
| [06-EXAMINER-QA.md](06-EXAMINER-QA.md) | 15 likely questions with evidence-backed answers |
| [07-NOVELTY-AUDIT.md](07-NOVELTY-AUDIT.md) | system-level prior-art audit: none of the four candidate distinctions is novel |
| [08-FIXED-WEIGHT-BASELINE.md](08-FIXED-WEIGHT-BASELINE.md) | the incumbent heuristic baseline, run: +0.0420 over the documented scheme (29/30), +0.0141 over the fitted eight-cell heuristic (27/30), which is the strongest baseline |

## Headline, as it may be stated

> On 30 service-disjoint partitions of a real 708-service production dependency
> graph, a learned probabilistic propagation layer over a SQL blast-radius
> system reaches PR-AUC 0.5519, against 0.3216 for the deterministic SQL
> reachability it annotates and 0.5099 for a documented hand-authored
> dependency-weight heuristic (**+0.0420, 29/30 partitions**). The strongest
> baseline is that same heuristic with its **eight weights fitted on training
> data**, at 0.5379 — ahead of logistic path features (0.5067), GraphSAGE
> (0.4724) and GCN (0.4691) — and against it the improvement is **+0.0141,
> 27/30 partitions**, while the fitted heuristic is better calibrated
> (ECE 0.1204 vs 0.1316) and slightly more precise (0.5073 vs 0.4977).
> Cascades are simulated, so these are not validated predictions of production
> failures; the corrected topology study failed two of its six pre-registered
> criteria; and no algorithmic novelty is claimed.

## Source artifacts (frozen, unmodified)

| artifact | contents |
|---|---|
| `ml/experiments/results/topology_v2.json` | the main study, 30 partitions × 3 inits |
| `ml/experiments/results/calibration_diagnostic.json` | raw vs isotonic-calibrated ECE |
| `ml/experiments/results/posweight_ablation.json` + `.npz` | class-weight arms, 1.19M retained predictions |
| `ml/experiments/results/quorum_control.json` | generator-confound control |
| `ml/experiments/results/direction*.json`, `incomplete*.json` | SCP-BiD and missing-edge studies |
| `docs/ml/*.md` | design, novelty, experiments, and the four study write-ups |

## Reproduce

```bash
python -m ml.experiments.run_topology_v2 --partitions 32 --inits 3   # ~176 min
python -m ml.experiments.analyse_topology_v2
python -m pytest ml/tests -q                                          # ML tests
cd backend && python -m pytest -q                                     # 45 API tests
cd tests && mysql --login-path=infratrace --table < run_all_tests.sql # 166 DB tests
```
