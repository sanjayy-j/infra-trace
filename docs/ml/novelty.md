# Novelty assessment

An honest account of what in SCP-GNN is new, what is not, and what still
needs checking.

---

## Verdict

**SCP-GNN is not a novel algorithm.** Its central mechanism — parameterising
Independent Cascade edge probabilities by *features* rather than by *edge
identity*, so the model generalises to unseen graphs and needs far less
training data — is the **hyperparametric Independent Cascade model**,
introduced by Kalimeris et al. (2018) for social networks and adapted to
**cascading failures** by Xiang et al. (2024) for power grids.

I arrived at the idea independently while working from the InfraTrace data,
and wrote the first version of this document claiming inductive
feature-parameterised cascade learning as the contribution. That claim was
wrong, and the literature search that found Xiang et al. came after the model
was already built. The claim is withdrawn.

What remains is an application and an evaluation, which is a smaller thing
and should be described as one.

---

## The prior art, specifically

### Kalimeris et al. (2018, 2019) — hyperparametric IC

Cited in detail by Xiang et al. §2–3. Each edge carries a feature vector
`x_uv` encoding properties of its endpoints; the influence probability is

```
p_uv = σ( θᵀ x_uv )
```

for a single global low-dimensional hyperparameter `θ`. The stated
motivation is exactly the one that drove this project: because the
probabilities are correlated through shared features, *"each observation
provides information about all edges in the network, thereby minimising
training data requirements"*.

### Xiang et al. (2024) — hyperparametric diffusion for cascading failures

[arXiv:2406.08522](https://arxiv.org/abs/2406.08522), *Predicting Cascading
Failures with a Hyperparametric Diffusion Model*. Applies the above to power
grids, with physics- and topology-based features on transmission lines.
Explicitly claims the property I had claimed: it can *"make diffusion
predictions and mitigate the risks of CFs even in unseen grid configurations,
for which existing methods cannot apply due to lack of training data."*

### Side-by-side

| | Xiang et al. 2024 | SCP-GNN (this work) |
|---|---|---|
| base process | Independent Cascade | Independent Cascade |
| edge probability | `σ(θᵀ x_uv)`, linear | `σ(MLP_θ([x_s; x_t; z_e]))`, 2-hidden-layer |
| features | physics + topology of transmission lines | `dependency_type`, `is_critical`, component type, criticality, degree |
| generalises to unseen graphs | yes, claimed | yes, measured |
| domain | power grids | software dependency graphs |
| learning | from failure traces, sparsifying a complete graph | BCE on known graph, end-to-end by gradient descent |
| graph | learned (not observable) | **given** — it is a table in the schema |

The methodological delta is **linear link → MLP**. That is a routine
generalisation, and the ablation in
[experiments.md](experiments.md#mlp-versus-linear-link) shows it is worth
nothing: a 28-parameter linear link matches the 1,441-parameter MLP
(+0.0016 PR-AUC, 95% CI [−0.011, +0.014], p = 0.78, winning 4 of 10 seeds).

**With that difference measured and found to be zero, SCP-GNN is the
hyperparametric Independent Cascade model.** Not a variant of it — the same
model, with a nonlinearity that the data does not use.

---

## What is established prior work, and is not claimed here

| component | prior work |
|---|---|
| Independent Cascade | Kempe, Kleinberg & Tardos, 2003 |
| Feature-parameterised ("hyperparametric") IC | Kalimeris et al., 2018 / 2019 |
| Hyperparametric IC for cascading failures | Xiang et al., 2024 (arXiv:2406.08522) |
| Noisy-OR as an aggregation rule | long established; learnability studied in arXiv:1612.08795 |
| Edge-type-conditioned message passing | R-GCN, Schlichtkrull et al., 2018 |
| Attention over edges | GAT, Veličković et al., 2018 |
| Training on synthetic graphs, transferring to real topologies | RiskNet, Rusek et al., 2022 (arXiv:2201.12263) |
| Per-edge IC parameters from observed cascades | Wilinski & Lokhov, 2020 (arXiv:2007.06557) |
| GNNs for microservice fault diagnosis / RCA | MicroHECL (arXiv:2103.01782); arXiv:2302.10512; and arXiv:2501.02766, which questions whether GNNs help at all |

That last entry is worth dwelling on: *"Are GNNs Actually Effective for
Multimodal Fault Diagnosis in Microservice Systems?"* (arXiv:2501.02766)
reaches a sceptical conclusion in a neighbouring problem. The result in
[experiments.md §3](experiments.md) — logistic regression on hand-engineered
path features matching the GNN on ranking — is consistent with it.

---

## What this work does contribute

Not algorithmic. In descending order of what I think they are worth:

1. **A measured structural ceiling on this problem.** 8 of 37 recorded
   impacts travel *forwards* along a dependency edge — a caller damaging
   what it calls (a rate limiter rejecting valid traffic; a retry storm
   hitting a database). Reverse traversal cannot express these, so recall is
   capped at **78.4%** for the existing SQL, for this model, and for any
   other method built on reverse reachability. I have not seen this
   quantified for software dependency graphs, and it is the most useful
   thing in this project: it says where the next gain actually is.

2. **A negative result worth reporting.** On 10 seeds and 40 unseen
   topologies, a logistic regression on path features is statistically
   indistinguishable from the GNN on PR-AUC (+0.003, p = 0.46, 5/10 seeds).
   Message passing is not what produces good ranking here. The GNN's real
   advantage is the operating point — recall 0.71 against 0.26 — which
   matters for paging but is a narrower claim than "better model".

3. **An ablation that separates two effects.** Edge semantics drive *ranking*
   (−0.053 PR-AUC without them, p < 0.001); the noisy-OR composition rule
   drives the *operating point* (recall collapses 0.71 → 0.16 under mean
   aggregation). These are usually bundled together as "edge-aware GNN"; here
   they are measured apart.

4. **Transmission rates measured on a real dependency schema**: synchronous
   critical 85.7%, synchronous non-critical 50%, asynchronous non-critical
   40%. Small-sample and from hand-authored seed data, but concrete.

5. **Engineering.** The reachability bound is proved and asserted in tests,
   so the model provably cannot hide a component the current SQL would flag;
   the database stays the source of truth with a test proving the Python
   traversal agrees with `fn_blast_radius_count`; the ML layer degrades to
   503 without taking the API down.

The reachability bound in (5) is a trivial corollary of IC on any graph and
is certainly folklore. It is listed as engineering, not as a finding.

---

## Still to verify

Honest gaps in the search behind this document. (The MLP-vs-linear question
that stood here has since been run; the answer is above.)

- **Kalimeris et al. 2018/2019 read only through Xiang et al.** I have the
  formulation and motivation from Xiang's §2–3, not from the originals.
  Before any external claim, read both directly.
- **No search for software-specific prior art** on hyperparametric IC. The
  searches covered microservice RCA, GNN fault diagnosis, IC learning and
  power-grid cascades. Something in dependency analysis, SRE or
  software-architecture venues may already apply this exact method to
  service graphs.
- **The forward-propagation finding is unverified against the literature.**
  Caller-damages-callee is surely known to SRE practitioners; whether it is
  quantified anywhere as a recall ceiling on dependency-graph impact
  analysis, I have not checked.
---

## If this were written up

The defensible framing is **not** "a new algorithm for blast-radius
prediction". It is:

> An application of hyperparametric Independent Cascade modelling to software
> dependency graphs, with an evaluation protocol that measures a structural
> recall ceiling imposed by dependency direction and finds that message
> passing does not improve ranking over hand-engineered path features on
> graphs of this depth.

That is a short workshop paper or an engineering report, not a novel method.
It would need the MLP-vs-linear ablation and a direct reading of Kalimeris
et al. before submission anywhere.
