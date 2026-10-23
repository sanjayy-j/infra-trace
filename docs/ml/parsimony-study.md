# Small-improvement study

Seven candidates investigated. **Six produced no measurable improvement.** The
seventh is not an accuracy improvement either — it is an *equivalence* result
whose payoff is interpretability.

No novelty is claimed anywhere in this document.

---

## 1. Candidates, and why most were eliminated before implementation

Each candidate was gated on a cheap premise check, because a candidate whose
premise fails cannot be rescued by implementation.

### Candidate A — decision-threshold selection

*Prior work:* cost-sensitive learning and threshold optimisation (Elkan 2001).
Entirely standard; no novelty available.

*Limitation it targets:* [experiments.md §4](experiments.md) records F1 of
0.512 at the default 0.5 threshold against 0.675 at the best threshold on real
incidents — an apparent 0.163 left on the table.

*Result:* **no effect.** Selecting the threshold on the validation split and
applying it, 10 seeds:

| | F1 @ 0.5 | F1 @ validation-selected | difference | p |
|---|---|---|---|---|
| synthetic held-out | 0.5247 | 0.5234 | −0.0013 | 0.90 |
| real incidents | 0.5805 | 0.5779 | −0.0026 | 0.91 |

A 5-seed pilot showed +0.031 on real data and that looked promising; at 10
seeds it vanished. The pilot was noise, and the larger run is the one to
believe.

Two further label-free rules were tried, on the theory that a *quantile*
transfers across prior shift better than an absolute cutoff:

| rule | F1 on real incidents |
|---|---|
| fixed 0.5 | 0.578 |
| validation absolute threshold | 0.609 |
| quantile transfer | 0.541 |
| expected-positive-count | 0.571 |
| oracle (test-selected) | 0.663 |

Both cleverer rules were **worse than doing nothing**. The oracle gap is real
but no label-free rule captures it, and with 66 decisions the oracle threshold
is itself overfit.

### Candidate B — criticality-aware prioritisation

*Premise check, run first:* does a component's recorded `criticality` predict
how badly it is actually impacted?

| criticality | impacts | mean severity (Unavailable 3 / Degraded 2 / Minor 1) |
|---|---|---|
| Medium | 6 | 2.00 |
| High | 8 | 1.88 |
| Critical | 23 | 1.83 |

**Flat, and if anything inverted.** Critical components do not suffer worse
impacts when hit. Weighting predictions by criticality has no empirical
support as a severity proxy, and inventing a business-cost model to justify it
would be manufacturing a result. *Eliminated without implementation.*

(Note the circularity this also avoids: scoring a criticality-weighted method
on a criticality-weighted metric would guarantee a "win" that means nothing.
The severity ground truth here is `impact_level`, a different column from
`criticality`.)

### Candidate C — uncertainty from sparse dependency information

*Prior work:* selective prediction / learning to reject (Chow 1970; Geifman &
El-Yaniv 2017). Standard.

*Premise check:* can any graph-structure signal predict the model's own errors
better than its confidence already does? ROC-AUC for predicting an error,
n = 1136 at 20% missing edges:

| signal | AUC |
|---|---|
| model confidence \|p−0.5\| | **0.680** |
| hop distance from root | 0.332 |
| target in-degree (sparsity) | **0.502** |
| target out-degree | 0.447 |

Target in-degree is at chance. The application-specific part — "flag
predictions made with thin dependency information" — **has no signal**. What
remains is textbook confidence-based abstention, which is Chow's rule with
nothing added. *Eliminated.*

### Candidate D — prior-shift correction

*Prior work:* Saerens, Latinne & Decaestecker (2002), EM estimation of a
shifted class prior from unlabelled predictions. Attractive because it uses no
target labels, so it cannot leak.

*Limitation it targets:* a measured shift — synthetic training prior 0.28
against real 0.44.

*Result:* **diverged.** The EM estimate collapsed to 1.000 against a true
0.439, pushing every prediction to 1. The apparent F1 gain (+0.065) is the
artefact of predicting everything positive; calibration was destroyed, ECE
0.080 → 0.561. SLD's EM is known to be unstable when the classes overlap
heavily, and 66 points with overlapping score distributions is that case.

---

## 2. The one finding that survived — and it is an equivalence, not a win

[novelty.md](novelty.md) established that the **28-parameter linear**
transmission function `σ(θᵀx)` matches the 1,441-parameter MLP on synthetic
data. This study extended that to transfer under distribution shift, 15 seeds,
training on synthetic and evaluating on the 22 real incidents:

| | synth PR-AUC | real PR-AUC | real F1 | real best-F1 | real ECE | params |
|---|---|---|---|---|---|---|
| MLP (current default) | 0.479 | 0.629 | 0.597 | 0.667 | 0.133 | 1,441 |
| Linear | 0.482 | 0.635 | 0.616 | 0.668 | 0.118 | **28** |

| difference (linear − MLP) | value | 95% CI | t | Wilcoxon |
|---|---|---|---|---|
| real **best-F1** | +0.0015 | [−0.009, +0.012] | 0.78 ns | 0.93 |
| real PR-AUC | +0.0060 | [−0.021, +0.033] | 0.64 ns | 1.00 |
| real F1 @ 0.5 | +0.0191 | [+0.0005, +0.038] | **0.045\*** | 0.083 ns |
| synth PR-AUC | +0.0030 | [−0.002, +0.008] | 0.25 ns | 0.36 |

**This is not a superiority claim, and the F1 row is why.** The only metric
reaching p < 0.05 is F1 at the fixed 0.5 cutoff, the two tests disagree about
it (t = 0.045, Wilcoxon = 0.083), and **best-F1 shows no difference whatever**
(p = 0.78). Equal best-F1 with unequal F1@0.5 means the models discriminate
identically and merely sit differently relative to an arbitrary threshold.
Reporting the F1 row as a win would be selecting the one favourable cell from
eight.

The defensible statement is: **the 1,441-parameter MLP buys nothing over 28
parameters**, in-distribution or under shift.

---

## 3. What the equivalence is actually worth

With a linear link the model *is* 28 numbers, and they can be read. Over 10
seeds:

| feature | weight | sign stable |
|---|---|---|
| `is_critical` | **+0.681** ±0.268 | 100% |
| `dep_type=Config` | −0.621 ±0.408 | 90% |
| `dep_type=Asynchronous` | **−0.565** ±0.220 | 100% |
| `dep_type=Synchronous` | **+0.289** ±0.146 | 100% |
| `dep_type=Data` | +0.162 ±0.168 | 100% |
| `dst:out_degree_norm` | −0.389 ±0.646 | 80% — unstable |
| `dst:in_degree_norm` | −0.316 ±0.609 | 70% — unstable |
| `src:type=Gateway` | −0.286 ±0.489 | 60% — unstable |

**Every edge feature is 100% sign-stable; several node features are not.** The
unstable ones are flagged rather than interpreted — a weight whose sign flips
between runs is noise regardless of magnitude.

### Independent corroboration

The learned ordering reproduces the rates measured on the real incidents,
which the model never sees during training:

| learned ranking | weight | measured on 22 real incidents |
|---|---|---|
| Synchronous + critical | **+0.969** | **85.7%** |
| Synchronous | +0.289 | 50% |
| Data | +0.162 | 50% |
| Asynchronous | −0.565 | 40% |

Rank agreement is exact. This is not a fit — training uses only synthetic
cascades — so it is corroboration that the model learned the right structure,
and it is checkable by anyone in one command.
[`test_learned_edge_ranking_matches_the_measured_rates`](../../ml/tests/test_ml.py)
fails the build if the ordering ever inverts.

```bash
python -m ml.explain --seeds 10
```

---

## 4. What changed, and what deliberately did not

| change | why |
|---|---|
| **new** `ml/explain.py` | reads coefficients from linear models; refuses an MLP rather than reporting a meaningless first layer |
| **new** `--linear-link` on `ml/train_production.py` | makes the 28-parameter configuration reproducible |
| `ml/predict.py` reads `linear_link` from the artifact | without it a linear artifact would be rebuilt as an MLP and fail to load; defaults to `False` so existing artifacts are unaffected |
| 5 new tests (34 total) | including the feature-name/width guard, which stops coefficients silently acquiring wrong labels |
| **unchanged:** the default model | the MLP artifact is the published baseline; switching it silently would invalidate every recorded result |

## 5. Recommendation

- **Use `--linear-link` for anything that needs explaining.** Same accuracy,
  51× fewer parameters, and 28 readable coefficients that independently
  reproduce the measured transmission rates. For a review, "here is what the
  model learned, and here is the real-world measurement it agrees with" is
  worth more than a point of F1.
- **Do not change the deployed default without re-running the benchmark.** The
  equivalence is measured, but the published numbers belong to the MLP artifact.
- **Stop looking for accuracy gains at this scale.** Seven candidates found
  nothing. The binding constraint is 22 real incidents on a 19-node graph, not
  the model. The next real gain is more data — which means instrumentation,
  as [incomplete-graph-study.md §6](incomplete-graph-study.md) argues.
