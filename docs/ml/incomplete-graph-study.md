# Research assessment — robustness to incomplete dependency graphs

Written before implementation. Results appended in §7.

**Headline for a reader in a hurry:** the phenomenon is real and matters, but
it is already established in the literature *with theory*, and the remedies
are established too. No novel method is claimed. What follows is an empirical
study with one minimal, testable mechanism, and a sharp prediction about why
the obvious cheap fix cannot work.

---

## 1. The problem is real, and it is already visible in the real data

Incident 22 ("Partner API returning stale shipment status") names a *shipment
status cache* in its root cause. Redis Cache is that cache. But no
`dependency` row records Shipping Service using Redis — so the impact is
unreachable from the root cause in **either** direction. The dependency table
is hand-maintained; it is incomplete, and we can prove one specific omission.

### How badly does this hurt?

Removing edges at random from the real 29-edge graph and recomputing how many
recorded impacts remain reachable from their root cause (30 trials per level):

| edges removed | edges left | recall ceiling |
|---|---|---|
| 0% | 29 | 78.4% |
| 5% | 28 | 75.6% |
| 10% | 26 | 72.6% |
| 20% | 23 | 61.8% |
| 30% | 20 | 56.6% |

Dropping three edges costs nearly six points of attainable recall.

### Why this is structural, not statistical

[design.md §4.2](design.md) proves that SCP-GNN assigns **exactly zero** to
any component unreachable from the root. On a complete graph that is a safety
property: the model can never flag something the SQL would not. On an
incomplete graph it is the opposite — a missing edge produces a confident
zero, with no way to hedge. The model cannot express "I don't see a path, but
these two components look like the sort of pair that usually has one."

The same bound that makes SCP-GNN safe to deploy makes it brittle to the one
data-quality problem we have already confirmed exists.

---

## 2. Prior art — the gap is smaller than it looks

### The phenomenon is established, with theory

**Chandrasekhar, Goldsmith-Pinkham, McCormick, Thau & Wei (2024),
[arXiv:2403.05704](https://arxiv.org/abs/2403.05704)**, *Non-robustness of
diffusion estimates on networks with measurement error*, shows that

> "even a vanishingly small share of missed links causes diffusion forecasts
> to be significant under-estimates"

and — the part that matters most for the experiment below — that the
**geometry of the missingness decides the outcome**: they give sufficient
conditions for *catastrophic* failure when missing links bridge distant
regions of the network (shortcuts), and sufficient conditions for
*robustness* when missing links are a uniformly random thinning that
preserves structure.

So the measurement in §1 is a replication, not a discovery. It also means a
study that only removes edges at random would be testing the case the theory
already says is benign.

**Belák, Mashhadi, Sala & Morrison (2015),
[arXiv:1502.01602](https://arxiv.org/abs/1502.01602)**, *Phantom cascades*,
establishes the same underestimation effect for hidden nodes and quantifies
it across five networks.

### The remedies are established too

| family | representative work | relevance |
|---|---|---|
| Graph structure learning | LDS, [arXiv:1903.11960](https://arxiv.org/abs/1903.11960); Graph-Revised Convolutional Network, [arXiv:1911.07123](https://arxiv.org/abs/1911.07123); GDN, [arXiv:2205.09575](https://arxiv.org/abs/2205.09575); survey [arXiv:2103.03036](https://arxiv.org/abs/2103.03036) | Learn or refine the adjacency jointly with the task. A large, mature field. Scoring candidate edges from node features is its standard move. |
| Network reconstruction from cascades | DiffStru [arXiv:2010.01400](https://arxiv.org/abs/2010.01400); [arXiv:1609.00432](https://arxiv.org/abs/1609.00432); [arXiv:1505.05663](https://arxiv.org/abs/1505.05663) | Infer missing links from many observed cascades. **Inapplicable here** — 22 incidents over 29 edges; most edges have never been observed carrying a failure. Same constraint that ruled out per-edge IC learning in [novelty.md](novelty.md). |
| Robustness regularisation | DropEdge, [arXiv:1907.10903](https://arxiv.org/abs/1907.10903) | Randomly drop edges during training. Cheap, standard, and the obvious first thing to try. |
| Influence maximisation on incomplete graphs | SP-GCRL, [arXiv:2605.12513](https://arxiv.org/abs/2605.12513) | Same setting, different task (seed selection, not impact prediction). |

### What actually remains

Not a method. Honestly:

1. Nobody appears to have measured this for **software dependency graphs**,
   where incompleteness arises from humans forgetting to record a dependency
   rather than from API sampling or privacy limits — and where we can point to
   the specific missing edge.
2. The **interaction between the reachability bound and incompleteness** is, as
   far as I can tell, not written down: a model with a hard structural bound
   fails differently from one without, and the failure is not graceful.

Both are empirical. **A novel algorithm is not justified by this literature**,
and none is claimed.

---

## 3. The one mechanism worth testing, and a sharp prediction

Problem analysis gives a prediction that is worth more than another
architecture, because it is falsifiable and cheap to test:

> **Training-time edge dropout cannot fix a structural zero.**
>
> DropEdge changes what the model *learns*. It cannot change the fact that at
> *inference*, if no path exists, every term in the noisy-OR product is
> `1 − p·0 = 1` and the output is exactly 0. Robustness training should
> therefore do nothing at all for the components that incompleteness actually
> loses. Only adding candidate edges to the propagation graph can help them.

If that prediction holds, it tells us something structural about this model
family and rules out the cheap fix on principle rather than by trial.

### Proposed mechanism — SCP-Latent

Keep SCP-GNN exactly. Add a second, low-probability edge set.

For every ordered non-adjacent pair `(s, t)` score a **latent dependency**
from node features alone:

```
ℓ_st = σ( MLP_φ( [ x_s ; x_t ] ) )          latent-edge plausibility
```

Keep the top-`k` per node, and admit them to the propagation graph with
transmission

```
p_st = ℓ_st · σ( MLP_θ( [ x_s ; x_t ; z_∅ ; 1 ] ) )
```

where `z_∅` is a zero edge-feature vector (we do not know the dependency type
of an edge we have not recorded) and the trailing `1` is a flag marking the
edge as latent. Observed edges keep `ℓ = 1` and their real features, so on a
complete graph the model reduces to SCP-GNN plus a small amount of noise.

Composition is unchanged — the same noisy-OR fixed point. Training is
unchanged except that input graphs have edges removed while **labels come from
the complete graph**, so `MLP_φ` is supervised to recover exactly the paths
that were hidden.

This is a feature-based latent-edge prior fused into the propagation. It is a
standard graph-structure-learning move, applied to a cascade model. **Not
novel; minimal and testable.**

### Why it should help, and where it should fail

It should help because it replaces a structural zero with a small hedge for
pairs that *look like* they should be connected. It should fail — and this
must be reported if it does — when:

- the latent scorer has nothing to go on, because component features do not
  predict dependencies well;
- the `k` extra edges per node cost more precision than the recovered recall
  is worth, which is exactly how [SCP-BiD](direction-study.md) failed;
- the missing edges are *shortcuts* between distant regions, which
  Chandrasekhar et al. show are the catastrophic case and which are also the
  hardest to guess from endpoint features.

---

## 4. Experimental design

**Ground truth comes from the complete graph.** Cascades are simulated on the
full generated graph, then edges are hidden from the *model input only*. The
labels therefore describe what really happened, including propagation through
edges the model cannot see. Hiding edges before simulating would change the
ground truth and measure nothing.

**Two removal regimes**, following Chandrasekhar et al.'s distinction:

| regime | how | expected |
|---|---|---|
| **random** | uniform thinning | theory says relatively benign |
| **targeted (bridge)** | remove highest edge-betweenness edges first — the shortcuts | theory says catastrophic |

A study with only the random regime would test the easy case. Removal
fractions 0 / 10 / 20 / 30%.

**Models.** Reachability · SCP-GNN (default) · SCP-BiD (experimental) ·
logistic on path features · GCN · **SCP-GNN + DropEdge training** (the cheap
established fix, and the test of the prediction in §3) · **SCP-Latent**
(proposed).

**Ablations.**

| ablation | isolates |
|---|---|
| SCP-Latent with `k` latent edges but **random** scoring instead of learned `ℓ` | whether the learned scorer matters, or merely having extra candidate edges does |
| SCP-Latent trained **without** edge removal | whether training-time hiding is what teaches the scorer |
| SCP-GNN + DropEdge | whether robustness training alone suffices (predicted: no) |

**Protocol.** Split by graph, 40 held-out topologies, 10 seeds, paired tests
with CIs. Metrics: PR-AUC, precision, recall, F1, calibrated ECE, parameters,
inference latency. Scored on the complete-graph reachable set so that all
models are judged on the same decisions, including the ones incompleteness
hides from them.

**A known asymmetry, recorded rather than hidden.** The neural models are
trained with masks derived from the COMPLETE graph, so they see (and are
penalised on) candidates the observed graph cannot reach. `LogisticBaseline`
fits with masks derived from the OBSERVED graph, because its `fit` takes one
graph and uses it for both features and masking. The effect should be close to
nil: logistic multiplies its output by observed reachability, so it predicts
exactly 0 on those candidates either way, and training on them could not have
changed that. It is noted because it is a real inconsistency in the protocol,
not because it is thought to matter.

**Real data.** The 22 incidents, with the same removal regimes applied to the
real graph. n = 22 is too small to settle anything; it is reported with
bootstrap CIs and treated as a sanity check, not evidence.

**Pre-registered success criterion.** SCP-Latent is an improvement only if, at
10–30% missing edges, it beats SCP-GNN on **both** PR-AUC and F1, and the
random-scoring ablation shows the gain comes from the learned scorer rather
than from extra edges. Recall bought entirely with precision is not an
improvement — that is how SCP-BiD failed, and the same bar applies here.

---

## 5. Why it fails — diagnosis

Run before looking at the headline numbers, because it predicts them.

### The latent scorer works, and that is not enough

A pure link-prediction probe: train `MLP_phi` on held-out topologies to
recover edges hidden at 25%, and score it on graphs it has never seen.

| removal regime | ROC-AUC | PR-AUC | no-skill PR-AUC |
|---|---|---|---|
| random | 0.836 | 0.079 | 0.021 |
| bridge | 0.872 | 0.102 | 0.021 |

The scorer is **four to five times better than chance** and ROC-AUC near 0.85
looks healthy. PR-AUC near 0.09 is what actually matters, and it is poor.

The cause is arithmetic, not architecture. Candidate pairs grow as **O(N²)**
while missing edges grow as **O(E) = O(N)**. On a typical generated graph that
is ~380 ordered non-adjacent pairs holding ~7 hidden edges: a **2.1% positive
rate**. A ranker at ROC-AUC 0.87 still puts many false candidates above the
true ones at that base rate, and every false one creates a propagation path
that was never there.

### No `top_k` rescues it

20% random removal, 3 seeds:

| top_k | latent edges added | PR-AUC | precision | recall | F1 |
|---|---|---|---|---|---|
| **0** | 0 | **0.365 ±0.021** | 0.346 | 0.598 | **0.437 ±0.023** |
| 1 | ~15 | 0.336 ±0.045 | 0.322 | 0.570 | 0.408 ±0.028 |
| 2 | ~30 | 0.326 ±0.036 | 0.316 | 0.586 | 0.406 ±0.010 |
| 4 | ~60 | 0.357 ±0.047 | 0.347 | 0.489 | 0.405 ±0.048 |

`top_k = 0` — admitting no latent edges at all — is the best setting. A typical
observed graph here has 15 nodes and 20 edges, so `top_k = 2` adds about 30
latent edges to recover roughly 5 hidden ones. The trade never pays.

This is the same failure shape as [SCP-BiD](direction-study.md): recall bought
entirely with precision.

### The exact accounting, by candidate class

Recall split by whether a true positive is still reachable on the observed
graph or was structurally lost by the edge removal. 20% random removal, 3
seeds, 384 positives.

| model | recall on still-reachable | recall on STRUCTURALLY LOST | new false positives |
|---|---|---|---|
| SCP-GNN | 207/314 = 65.9% | **0/70 = 0.0%** | — |
| SCP-GNN + DropEdge 0.2 | 207/314 = 65.9% | **0/70 = 0.0%** | — |
| SCP-Latent `top_k=0` | 218/314 = 69.4% | **0/70 = 0.0%** | 0 |
| SCP-Latent `top_k=2` | 203/314 = 64.6% | 7/70 = **10.0%** | **23** |
| SCP-Latent `top_k=4` | 214/314 = 68.2% | 5/70 = 7.1% | 31 |

**The prediction in §3 holds exactly.** DropEdge recovers *none* of the 70
structurally-lost positives — identical to plain SCP-GNN, to the component.
Robustness training cannot reach what the graph cannot reach, as the noisy-OR
algebra requires. Any gain it shows elsewhere is regularisation on the
reachable set.

**The latent mechanism does work, and still loses.** `top_k=2` is the only
configuration that recovers any of the lost components at all — 7 of 70. It
pays for them with 23 new false positives **and** 15 still-reachable true
positives it no longer finds (218 → 203), because admitting ~30 spurious edges
dilutes the probability mass along the real paths. Seven recovered against
fifteen lost and twenty-three invented is not a trade worth making, and no
`top_k` changes its sign.

### The signals that might have helped are already in the features

Two structural regularities exist in the real schema:

- components sharing an application are **1.7×** more likely to depend on each
  other (13.0% against 7.8%);
- dependency types are strongly ordered — Database→Service, Queue→Service,
  Service→Gateway. Datastores never depend on anything.

Both are already available to the scorer as `component_type` one-hots and
degree features, which is *why* it reaches ROC-AUC 0.87. Adding them again
explicitly cannot move a 2.1% base rate.

### A correction made during the study

The first implementation targeted "bridge" edges by **undirected** edge
betweenness. Measured over 12 graphs at 25% removal, that destroyed 37
reachable components against random removal's 38 — statistically
indistinguishable. The targeted arm was quietly testing the benign case.

Failure propagates along **directed** paths, so an edge can sit on many
undirected shortest paths and carry no directed ones. Switching to directed
edge betweenness destroyed **61**. All results below use the directed
variant, and
[`test_bridge_removal_hurts_more_than_random_removal`](../../ml/tests/test_ml.py)
fails the build if the targeted regime ever stops being harder.

## 6. What the diagnosis implies for the alternatives

The §5 arithmetic rules out a whole family of approaches, which is more useful
than the negative result on its own.

**Edge recovery is the wrong goal.** Anything that tries to *identify which
specific dependency is missing* faces the same 2.1% base rate: O(N²)
candidates against O(N) missing edges. Better features, a bigger scorer, or a
full graph-structure-learning stack all move the ranking, not the base rate.
Graph structure learning works where the signal-to-candidate ratio is far
better than this.

**Uncertainty-aware prediction is the right reframing, and the diagnosis is
what makes it attractive.** It does not need to find the missing edge. It only
needs to notice that the graph *looks* incomplete — a component with
suspiciously low recorded degree, a tier with fewer dependencies than its
peers — and widen its predictive interval accordingly. "I think this component
is safe, but my view of its dependencies is thin" is actionable and achievable
at a 2.1% base rate, whereas "the missing edge is specifically X→Y" is not.

This was not implemented here. Evaluating an uncertainty estimate properly
needs proper scoring rules and far more than 22 real incidents, and claiming
an improvement on that basis would be exactly the manufactured confidence this
study is trying to avoid.

**The honest engineering answer is not an algorithm.** The dependency table is
hand-maintained; that is the defect. Distributed tracing or a service mesh
observes the real call graph directly and removes the guesswork — which is
precisely what AID ([arXiv:2109.04893](https://arxiv.org/abs/2109.04893)) uses.
Spending modelling effort to infer what instrumentation would simply tell you
is the wrong trade, and the measurements here are what make that case
concretely rather than as an opinion.

## 7. Results

**SCP-Latent fails the pre-registered criterion.** It had to beat SCP-GNN on
both PR-AUC and F1 at 10-30% missing edges. It beats it in **none** of the six
removal configurations, and loses to its own `top_k=0` ablation in all of
them. It is not deployed and is not recommended.

### Recall ceiling per configuration

How many true positives are still reachable on the observed graph. This is the
most any method can get, and it confirms the targeted regime is the harder one:

| config | ceiling |
|---|---|
| random 0% | 100.0% |
| random 10% / 20% / 30% | 87.8% / 81.8% / 70.6% |
| **bridge** 10% / 20% / 30% | **85.9% / 72.4% / 61.2%** |

Removing bridge edges costs ~9 points more than random thinning at 20-30%,
exactly as Chandrasekhar et al. (2024) predict.

### PR-AUC, 10 seeds, 32 held-out topologies per config

| model | rand 0% | rand 10% | rand 20% | rand 30% | bridge 10% | bridge 20% | bridge 30% |
|---|---|---|---|---|---|---|---|
| Reachability (observed) | 0.268 | 0.270 | 0.280 | 0.287 | 0.300 | 0.314 | 0.313 |
| Logistic (path feats) | **0.458** | **0.443** | 0.423 | 0.409 | **0.449** | **0.431** | **0.409** |
| GCN | 0.393 | 0.376 | 0.353 | 0.345 | 0.357 | 0.344 | 0.321 |
| SCP-GNN (default) | 0.456 | 0.435 | **0.430** | 0.406 | 0.439 | 0.426 | 0.393 |
| SCP-BiD (experimental) | 0.458 | 0.398 | 0.398 | 0.375 | 0.401 | 0.389 | 0.370 |
| SCP-GNN + DropEdge 0.2 | 0.452 | 0.436 | 0.429 | **0.412** | 0.441 | 0.423 | 0.392 |
| **SCP-Latent (proposed)** | 0.455 | 0.417 | 0.403 | 0.384 | 0.428 | 0.388 | 0.370 |
| *ablation:* random scoring | 0.457 | 0.415 | 0.394 | 0.393 | 0.426 | 0.404 | 0.377 |
| *ablation:* `top_k=0` | 0.460 | 0.438 | 0.428 | 0.413 | 0.440 | 0.421 | 0.393 |

F1 at threshold 0.5:

| model | rand 0% | rand 10% | rand 20% | rand 30% | bridge 10% | bridge 20% | bridge 30% |
|---|---|---|---|---|---|---|---|
| SCP-GNN (default) | 0.496 | 0.468 | 0.468 | 0.448 | 0.458 | 0.461 | 0.441 |
| SCP-GNN + DropEdge 0.2 | 0.488 | 0.486 | 0.452 | 0.446 | 0.486 | 0.474 | 0.449 |
| **SCP-Latent (proposed)** | 0.513 | 0.466 | 0.454 | 0.431 | 0.472 | 0.442 | 0.433 |
| *ablation:* `top_k=0` | 0.509 | 0.479 | 0.467 | 0.451 | 0.492 | 0.475 | 0.437 |

### Four findings

**1. The proposal is worst exactly where it was supposed to help.** Its deficit
against SCP-GNN grows with the amount of missing structure:

| removal | 0% | 10% | 20% | 30% |
|---|---|---|---|---|
| random | −0.001 | −0.018 | −0.027 | −0.022 |
| bridge | — | −0.011 | **−0.038** | −0.023 |

At 0% it is tied, as the design requires (`top_k=0` reduces to SCP-GNN). Every
removal level makes it relatively worse.

**2. The learned scorer is not merely useless — at times it is worse than
random.** The random-scoring ablation beats the learned one at random 30%
(0.393 vs 0.384), bridge 20% (0.404 vs 0.388) and bridge 30% (0.377 vs 0.370).
A scorer with ROC-AUC 0.87 selecting from a 2.1%-positive pool concentrates its
picks in a plausible-looking region, and a wrong edge there creates a more
convincing false path than a random one does.

**3. DropEdge is the better cheap fix, and the prediction about it held.** It
matches or slightly beats SCP-GNN on F1 at every level, at zero inference cost.
But §5 shows it recovers **0 of 70** structurally-lost positives, identical to
plain SCP-GNN to the component: its gain is regularisation on the still-
reachable set, exactly as predicted. It does not address the failure mode; it
just does not make anything worse.

**4. Reachability's rising PR-AUC is an artefact, not an improvement.** Its
PR-AUC climbs from 0.268 to 0.313 as edges are removed. That is only because it
flags fewer components, so precision rises (0.268 → 0.340) while recall
collapses (1.000 → 0.614). Missing dependencies do not improve the SQL.

### Real incidents (n = 22)

Models trained on synthetic data with the matching removal regime; labels from
the real incident records.

| config | ceiling | SCP-GNN | SCP-Latent |
|---|---|---|---|
| none (complete) | 100.0% | 0.718 [0.592, 0.870] | 0.747 [0.627, 0.881] |
| random 20% | 72.4% | **0.707** [0.602, 0.835] | 0.654 [0.531, 0.802] |
| bridge 20% | 75.9% | **0.722** [0.615, 0.858] | 0.686 [0.586, 0.815] |

Consistent with the synthetic result: tied on the complete graph, worse under
both removal regimes. The intervals overlap heavily and n = 22; this does not
independently establish anything, it just fails to contradict.

### Recommendation

- **Keep SCP-GNN as the default.** Nothing here beats it under incompleteness.
- **Do not deploy SCP-Latent.** It loses in all six removal configurations.
- **Consider DropEdge** if a small F1 gain is wanted: free at inference, never
  harmful here. But it does not fix the structural failure.
- **The real fix is instrumentation, not modelling** — see §6.
