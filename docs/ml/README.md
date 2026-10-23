# InfraTrace ML layer — setup and usage

Probabilistic blast-radius prediction on top of the existing SQL.

**Read first:** [novelty.md](novelty.md). The model is not a novel algorithm
— it is the hyperparametric Independent Cascade model (Kalimeris et al.
2018), applied to software dependency graphs. The contribution of this work
is the evaluation, not the method.

| document | contents |
|---|---|
| [design.md](design.md) | problem, formulation, maths, architecture, failure modes |
| [experiments.md](experiments.md) | protocol, results, ablations, limits |
| [novelty.md](novelty.md) | prior art, what is and is not new |
| [direction-study.md](direction-study.md) | follow-up study on forward propagation — a negative result, with the one regime where it wins |
| [incomplete-graph-study.md](incomplete-graph-study.md) | follow-up study on missing dependencies — a negative result, with the arithmetic that explains it |
| [parsimony-study.md](parsimony-study.md) | seven small-improvement candidates — six null, one equivalence result that buys interpretability |
| [topology-study.md](topology-study.md) | real production topology from ChronoGraph — whether earlier conclusions survive a realistic substrate |

Both follow-up studies are **negative results**. SCP-GNN remains the default
because nothing tried has beaten it; SCP-BiD and SCP-Latent are kept as
experimental baselines so the next attempt starts from measured ground rather
than from scratch.

---

## What it adds

The SQL blast radius returns every component **reachable** from a failure.
On the 22 recorded incidents that is 66 components where 37 were actually
affected — precision 43.9%. This layer attaches a probability to each one,
so an operator can page on confidence rather than on reachability.

```
GET /components/11/blast-radius              →  3 components     (SQL, unchanged)
GET /components/11/blast-radius/predicted    →  3 components with probabilities
                                                Payment Service  0.57
                                                Order Service    0.29
                                                API Gateway      0.22
```

The predicted endpoint never adds a component the recursive CTE did not
find, and never removes one — it scores them. See
[design.md §4.2](design.md) for why that is guaranteed rather than observed.

---

## Install

The ML layer is **optional**. Without it the API serves its other 27
endpoints normally and the two `/ml` routes return 503.

```bash
pip install -r ml/requirements.txt
```

torch (CPU), numpy, scikit-learn, scipy. No torch-geometric — the noisy-OR
aggregator has no PyG equivalent and the baselines are a few lines each on
scatter operations.

---

## Train

```bash
python -m ml.train_production                 # ~10 s on CPU
python -m ml.train_production --epochs 200 --seed 3
python -m ml.train_production --out ml/artifacts/experiment.pt
```

Writes `ml/artifacts/scp_gnn.pt`: weights, architecture config, an isotonic
calibrator fitted on validation, and provenance (UTC timestamp, seed, data
settings, torch version, validation metrics).

Training uses **synthetic** graphs, never real incidents — those are held
back for evaluation. The artifact is gitignored; it is build output.

> If you want the simplest defensible model, use `--linear-link`
> (28 parameters). Measured as equivalent to the default MLP in-distribution
> *and* under transfer to real incidents — see
> [parsimony-study.md](parsimony-study.md) — and its coefficients are readable:
>
> ```bash
> python -m ml.explain --seeds 10
> ```
>
> The learned ordering (Synchronous+critical ≫ Synchronous > Data ≫
> Asynchronous) independently reproduces the rates measured on the 22 real
> incidents, which the model never sees during training.

## Predict

```python
from ml.extract import load_graph
from ml.predict import load_predictor

graph = load_graph()                    # from MySQL
predictor = load_predictor()

for row in predictor.predict(graph, component_id=11):
    print(row["component_name"], row["predicted_probability"])

for e in predictor.explain(graph, 11):  # per-edge transmission probabilities
    print(e.source_name, "->", e.target_name, e.transmission_probability)
```

## Serve

Nothing to do — `backend/app/main.py` mounts the router automatically when
torch and an artifact are present.

```bash
cd backend && python -m uvicorn app.main:app --reload
```

| endpoint | returns |
|---|---|
| `GET /components/{id}/blast-radius/predicted?threshold=0.5` | per-component probabilities, per-edge explanation, model provenance, caveats |
| `GET /ml/model` | provenance and validation metrics of the loaded model |

The dashboard shows this in **Dependency Explorer → Predicted impact**, in a
deliberately different colour from the SQL cards, because everything else on
that page is an observed fact and this one card is an estimate.

---

## Reproduce the experiments

```bash
python -m ml.experiments.run_benchmark --seeds 10     # ~25 min CPU; writes results/benchmark.json
python -m ml.experiments.run_benchmark --seeds 3 --quick   # ~2 min smoke run
python -m ml.experiments.run_real_eval --seeds 5      # needs MySQL

# directionality study (SCP-BiD)
python -m ml.experiments.run_direction --seeds 10
python -m ml.experiments.run_direction_real --seeds 5      # needs MySQL

# incomplete-graph study (SCP-Latent)
python -m ml.experiments.run_incomplete --seeds 10
python -m ml.experiments.run_incomplete_real --seeds 3     # needs MySQL

# topology-realism study (real ChronoGraph structure)
python -m ml.experiments.run_topology --seeds 10
```

The topology study uses a real 708-service dependency graph vendored at
`ml/data/chronograph/` under Apache-2.0 (see its `SOURCE.md`). Only the
TOPOLOGY is real — cascades over it are still simulated, so those results
isolate the effect of structure and are not real-world validation.

Forward propagation is **off by default** in the generator, so the first two
commands reproduce the published numbers exactly. The direction experiments
enable it explicitly.

## Test

```bash
python -m pytest ml/tests -q      # 17 tests; DB-backed ones skip without MySQL
cd backend && python -m pytest -q  # 45 existing API tests, unaffected
```

The two that matter:

- `test_prediction_never_exceeds_reachability` — the model cannot flag a
  component the SQL would not. Checked on random and trained weights.
- `test_python_reachability_matches_sql` — `ml/graph.py` keeps a second
  traversal for synthetic graphs; this proves it agrees with
  `fn_blast_radius_count` on every real component.

---

## Known limits

Full list in [experiments.md §5](experiments.md). The four that matter most:

1. **Recall is capped at 78.4%.** 8 of 37 real impacts are unreachable by
   reverse traversal — mostly a caller damaging what it calls. Modelling the
   second direction was tried ([direction-study.md](direction-study.md)): it
   lifts the ceiling to 97.3% but halves precision, so it is not deployed.
2. **Trained on synthetic data.** Real incident data is 22 scenarios on one
   19-node graph; it cannot train anything.
3. **Not better than simple baselines on the real graph.** On 22 incidents a
   one-parameter depth heuristic is competitive. The real graph is 3 hops
   deep, and depth alone nearly separates the classes.
4. **Calibration transfers imperfectly.** Synthetic positive rate ~26% versus
   real 44%, so 0.5 is the wrong threshold. Refit the isotonic calibrator on
   local incident history before trusting the absolute numbers.
