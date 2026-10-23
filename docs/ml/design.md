# SCP-GNN — Semantic Cascade Propagation

Algorithm specification for the InfraTrace ML layer.

---

## 1. The problem, and why the existing system does not solve it

InfraTrace already answers *"if component `r` fails, what else is affected?"*
with a recursive CTE ([`07_blast_radius.sql`](../../database/queries/07_blast_radius.sql)),
which walks `dependency` in reverse — from `depends_on_id` back to
`component_id` — and returns every component it can reach.

That computes **reachability**, which is a different question from the one an
on-call engineer actually asks. Reachability treats every dependency as
fatal. Measured against the 22 recorded ShopSphere incidents:

| | value |
|---|---|
| components flagged | 66 |
| components actually affected | 37 |
| correctly flagged | 29 |
| **precision** | **43.9%** |
| **recall** | **78.4%** |

More than half of what it pages is noise.

The variation it is blind to is large. Splitting the 1-hop edges out of a
root cause by their recorded semantics:

| dependency type | critical | exposures | propagated | rate |
|---|---|---|---|---|
| Asynchronous | yes | 2 | 2 | 100.0% |
| Synchronous | yes | 14 | 12 | **85.7%** |
| Synchronous | no | 14 | 7 | 50.0% |
| Data | no | 2 | 1 | 50.0% |
| Asynchronous | no | 10 | 4 | **40.0%** |

A synchronous critical dependency carries failure roughly twice as often as
an asynchronous non-critical one. The schema records that distinction in
`dependency_type` and `is_critical`; the recursive CTE cannot use it, because
reachability is a boolean property of the graph.

### The recall ceiling — a limit no model on this graph can beat

Recall is 78.4%, not 100%, because **8 of the 37 recorded impacts are on
components that are not reachable from the root cause at all**. These are
real and they are not data errors:

| incident | root cause | impact | direction |
|---|---|---|---|
| 11 | API Gateway | Order Service, Search Service | Gateway *depends on* them |
| 18 | Payment Service | Payment DB, Stripe Payment API | Service *depends on* them |
| 9, 15 | Recommendation / Notification Service | Kafka Event Bus | service *depends on* Kafka |
| 14, 22 | Authentication / Shipping Service | Redis Cache | service *depends on* Redis |

In most of these the failure travelled **forwards** along the dependency edge
— a misbehaving caller damaged the thing it calls (a retry storm hammering a
database, a producer writing bad messages). Reverse traversal cannot express
this.

Checking each case individually afterwards showed the picture is not uniform:
5 are forward propagation proper, 2 (incident 11) are the same incident
recording loss of a *user-visible capability* rather than component failure,
and incident 22 is unreachable in either direction because the graph is
**missing an edge**. See [direction-study.md §1](direction-study.md).

So **78.4% is a ceiling on recall** for the current SQL, for SCP-GNN, and for
any other method built on reverse reachability.

Raising it needs a second edge direction in the model. That was built and
measured — see [direction-study.md](direction-study.md). It lifts the ceiling
to 97.3% but grows the candidate set 6× and halves precision, so it improves
ranking and worsens F1. SCP-GNN remains the default.

---

## 2. Problem statement

Let `G = (V, E)` be the dependency graph. A row
`dependency(component_id = u, depends_on_id = v)` means *u depends on v*, so
failure propagates `v → u`. All of the following uses **propagation edges**,
`(s → t)` where `t` depends on `s`.

> **Prediction target.** Given a root cause `r ∈ V`, estimate for every
> `v ∈ V \ {r}` the probability `q_v = P(v fails | r fails)`.

This is a *forward* problem — given the cause, predict the consequences. It
is the opposite of microservice root-cause localisation (MicroHECL,
arXiv:2103.01782 and similar), which observes symptoms and infers the cause.

### Features and labels

| | source | contents |
|---|---|---|
| node `x_v` (11 dims) | `component` | `component_type` one-hot (7), `criticality` ordinal, `is_active`, normalised in/out degree |
| edge `z_{s→t}` (5 dims) | `dependency` | `dependency_type` one-hot (4), `is_critical` |
| label `y_v` | `incident_component` | 1 if recorded as affected in this incident |

**No feature is derived from incident history.** This is deliberate.
`fn_incident_count` and `fn_risk_score` both read incident data, and using
either as a feature would leak the label: a component's past incident count
partly determines whether it appears in `incident_component`. Degree is
computed from `dependency` only.

Degrees are `log1p`-compressed and divided by `log1p(N-1)` so a model trained
on 20-node graphs transfers to 200-node ones instead of seeing out-of-range
inputs.

---

## 3. The model

### 3.1 Transmission function

For each propagation edge `e = (s → t)`:

```
p_e = σ( MLP_θ( [ x_s ; x_t ; z_e ] ) )        clamped to [ε, 1−ε], ε = 1e−6
```

`MLP_θ` is `(2·11 + 5) → 32 → 16 → 1`, ReLU, dropout 0.1 — **1,441
parameters in total, independent of graph size**.

`p_e` is interpreted as *the probability that a failure of `s` is carried
across this dependency to `t`*.

### 3.2 Propagation — noisy-OR

Let `q_v^(k)` be the probability `v` has failed after `k` rounds.

```
q_r^(0) = 1                     (the root cause is the premise)
q_v^(0) = 0        for v ≠ r

q_v^(k) = 1 − (1 − q_v^(k−1)) · ∏ ( 1 − p_{s→v} · q_s^(k−1) )
                                 (s→v) ∈ E
```

This is the marginal of an Independent Cascade process under the usual
independence assumption: `v` survives round `k` only if it was already
surviving **and** every incoming dependency independently failed to carry
the failure.

Implemented in log-survival space for numerical stability, since the product
runs over a node's whole in-neighbourhood:

```
S_v^(k) = log(1 − q_v^(k))

S_v^(k) = min( S_v^(k−1),  Σ log1p( −p_{s→v} · q_s^(k−1) ) )
                           (s→v) ∈ E
```

The `min` enforces monotonicity: a node that has become more likely to fail
can never become less likely in a later round, which makes the iteration
converge rather than oscillate. `q_v = 1 − exp(S_v)`.

Same `p_e` every round — this is a **fixed-point iteration, not a stack of
layers**. Depth costs no parameters, and `rounds` can be raised at inference
time to cover chains longer than those seen in training.

### 3.3 Training

Binary cross-entropy over the evaluation mask, positives weighted by the
training split's negative/positive ratio (clipped to `[0.2, 20]`):

```
L = mean over masked v of   w_v · BCE( q_v , y_v ) ,    w_v = pos_weight if y_v = 1 else 1
```

The mask excludes the root, and by default excludes components unreachable
from it — those are forced to exactly 0 by the propagation rule, so they
contribute no gradient and would otherwise flood the loss with free true
negatives. Adam, lr 0.01, weight decay 1e-4, gradient clipping at 5.0, early
stopping on validation PR-AUC with patience 20.

`pos_weight` trades calibration for recall; an isotonic map fitted on the
**validation** split repairs the scale afterwards. Both the raw and
recalibrated numbers are reported — see
[experiments.md](experiments.md#calibration).

### 3.4 Inference

`ml/predict.py` loads the artifact, rebuilds the graph from MySQL, runs one
forward pass, applies the calibrator, and returns one row per reachable
component. 0.21 ms per scenario on CPU.

---

## 4. The three properties this buys

### 4.1 Inductive, not transductive — and not novel

Classical Independent Cascade learning fits **one free parameter per edge**
from many observed cascades on a fixed graph (Wilinski & Lokhov 2020,
arXiv:2007.06557, via dynamic message passing). That cannot be applied here
at all: InfraTrace has 22 incidents over 29 edges, so most edges have never
been observed carrying a failure, and a dependency graph changes every
sprint anyway.

Parameterising `p_e` by edge **features** rather than edge **identity** makes
the parameter count independent of `|V|` and `|E|`, and lets the model score
a dependency added five minutes ago.

> **This property is prior work, not a contribution.** It is the
> *hyperparametric Independent Cascade* model of Kalimeris et al. (2018),
> applied to cascading failures by Xiang et al. (2024,
> [arXiv:2406.08522](https://arxiv.org/abs/2406.08522)) with the same
> data-scarcity motivation. The only methodological difference in this
> implementation was an MLP in place of their linear `σ(θᵀx)`, and
> [the ablation](experiments.md#mlp-versus-linear-link) shows that difference
> is worth nothing. See [novelty.md](novelty.md).

### 4.2 Bounded by reachability — provably

**Claim.** If `v` is unreachable from `r`, then `q_v = 0` exactly, at every
`k`.

**Proof.** By induction on `k`. At `k = 0`, `q_v^(0) = 0` for all `v ≠ r`.
Assume `q_s^(k−1) = 0` for every `s` unreachable from `r`. For unreachable
`v`, every in-neighbour `s` is also unreachable (otherwise `v` would be
reachable via `s`), so every product term is `1 − p · 0 = 1`; the product is
1, and `q_v^(k) = 1 − (1 − 0)·1 = 0`. ∎

So the model can only ever **prune** the SQL's false positives — it can never
invent an affected component the recursive CTE did not already find. Adopting
it cannot lose a true positive the current system would have caught.

Asserted directly in
[`test_prediction_never_exceeds_reachability`](../../ml/tests/test_ml.py), on
both random and trained weights, because the property must come from the
propagation rule rather than from what the model happened to learn.

### 4.3 The intermediate quantities are the explanation

`p_e` is a probability on a real dependency edge, and the forward pass
multiplies exactly those numbers together. So:

> *Order Service is at 0.29 because the edge carrying the failure into
> Payment Service transmits at 0.73, and the edge from there transmits at
> 0.66.*

is a literal description of the computation, not a post-hoc attribution. No
SHAP or counterfactual explainer is involved, and none is needed. The API
returns these as `edge_transmission`.

---

## 5. Where it should fail

Noisy-OR assumes each incoming dependency transmits **independently**. A
component that only falls over when a *quorum* of its dependencies fail —
`k`-of-`n`, a threshold rule — is outside the hypothesis class and cannot be
represented exactly at any parameter setting.

This is not hypothetical: [`ml/synth.py`](../../ml/synth.py) generates such
nodes **on purpose**, so the training data lies partly outside the model's
own hypothesis class and the benchmark is not measuring a model fitting data
drawn from its own assumptions. See
[experiments.md](experiments.md#the-circularity-trap).

Other known limits:

- **Direction.** Only reverse propagation; the 8 forward-direction impacts in
  §1 are structurally invisible.
- **No redundancy model.** Replicas, failover, circuit breakers and live
  health are not in the schema, so they are not in the features. Inherited
  from the SQL, not introduced here.
- **No time.** `q_v` is a terminal probability. It says nothing about *when*
  a component fails, or in what order — `deployment.deployed_at` and
  `incident.started_at` are available and unused.

---

## 6. Files

| file | role |
|---|---|
| [`ml/graph.py`](../../ml/graph.py) | graph container, feature encoding, reachability |
| [`ml/synth.py`](../../ml/synth.py) | synthetic estates and ground-truth cascades |
| [`ml/extract.py`](../../ml/extract.py) | real graph + incidents from MySQL; SQL agreement check |
| [`ml/models/scp.py`](../../ml/models/scp.py) | the proposed model |
| [`ml/models/baselines.py`](../../ml/models/baselines.py) | reachability, depth decay, logistic, GCN, GraphSAGE, GAT |
| [`ml/train.py`](../../ml/train.py) | shared training loop |
| [`ml/evaluate.py`](../../ml/evaluate.py) | metrics, calibration |
| [`ml/predict.py`](../../ml/predict.py) | inference and explanations |
| [`ml/train_production.py`](../../ml/train_production.py) | train and save a versioned artifact |
| [`backend/app/ml_api.py`](../../backend/app/ml_api.py) | the two API endpoints |
