# The fixed-weight heuristic baseline

This closes the gap that [07-NOVELTY-AUDIT.md](07-NOVELTY-AUDIT.md) found in
our own evidence: the obvious incumbent baseline — a hand-authored dependency
multiplier table of the kind IncidentRelay documents — had never been run, so
"a learned propagation model beats hand-set edge weights" was an unsupported
claim. It has now been run, on exactly the frozen partitions.

**Headline: the claim now holds, and our previously-reported margin was
overstated.** SCP-GNN beats the documented heuristic by +0.0420 PR-AUC in
29/30 partitions. But an eight-cell version of the same heuristic, fitted on
the training pool, reaches PR-AUC **0.5379** — better than every baseline in
the original study — which cuts SCP-GNN's lead over the strongest baseline
from **+0.0452 to +0.0141**, and that fitted table is *better calibrated and
slightly more precise* than SCP-GNN.

---

## Why the comparison is fair

Nothing was retrained and no frozen artifact was touched. SCP-GNN's numbers
are read from `topology_v2.json` as stored. The test data was **regenerated,
not re-sampled**: `evaluate_partition` consumes one `default_rng(part.seed)`
stream in a fixed order — train, val, TEST-R, TEST-H — entirely before any
torch seeding, so replaying that order reproduces the same subgraphs, the same
cascades and the same labels.

That is verified rather than assumed. Each partition recomputes the three
deterministic models from the frozen run — reachability, plus depth-decay and
logistic *fitted on the regenerated training pool* — and halts unless all five
metrics match the stored values to **1e-12**. All 30 partitions matched
exactly, and the two partitions the frozen run found unusable (seeds 11, 25)
were found unusable here for the same reason. The heuristic then scores through
the same `collect_predictions` mask and the same `summarize` metric code as
SCP-GNN did.

## The four arms

Frozen in `ml/heuristic.py` before any result was computed.

| arm | weights | combination rule | what it answers |
|---|---|---|---|
| **H-IR-noisyOR** | IncidentRelay's documented multipliers | noisy-OR (SCP-GNN's) | isolates the *weights*, rule held fixed |
| **H-IR-maxProduct** | IncidentRelay's documented multipliers | max-product (IncidentRelay's own) | **the deployable incumbent** |
| **H-ORACLE** | the generator's own per-type rates | noisy-OR | upper bound for any fixed table here. **Not deployable** |
| **H-FIT** | 9 cells fitted on train/val | noisy-OR | information-matched: same loop, loss, epochs, early stopping as SCP-GNN |

The arm is labelled `H-FIT (8 params)` in the code and the artifact because
**8 of its 9 cells are used**: the 9th is an `unknown-type` fallback for a
dependency whose type is outside the schema vocabulary. It finished at exactly
its 0.500 initialisation in every partition, which is independent proof that
no such edge exists in this data — every edge routed to one of the 8 real
(type × criticality) cells.

Every arm sees **only** edge type, criticality and topology — never component
type, criticality, is_active or degree, and it cannot condition an edge on its
endpoints. A test asserts this by perturbing every node feature and requiring
bitwise-identical output. SCP-GNN sees all of it, so its win over H-FIT
measures node context *plus* the MLP, and its win over H-ORACLE measures that
it beats a table handed the right numbers.

## Results — TEST-R, mean over the same 30 partitions

| model | PR-AUC | precision | recall | F1 | ECE |
|---|---|---|---|---|---|
| Reachability (SQL, deployed) | 0.3216 | 0.3216 | 1.0000 | 0.4845 | 0.6784 |
| Logistic (path features) | 0.5067 | **0.5685** | 0.2920 | 0.3771 | **0.0378** |
| **H-IR-maxProduct** (incumbent) | 0.5099 | 0.3564 | 0.9696 | 0.5204 | 0.4251 |
| **H-IR-noisyOR** | 0.5216 | 0.3524 | 0.9734 | 0.5166 | 0.4603 |
| **H-FIT (fitted eight-cell) — strongest baseline** | **0.5379** | 0.5073 | 0.6255 | 0.5524 | 0.1204 |
| H-ORACLE *(not deployable)* | 0.5425 | 0.4802 | 0.7015 | 0.5686 | 0.1682 |
| **SCP-GNN** | **0.5519** | 0.4977 | 0.6826 | **0.5696** | 0.1316 |

GCN 0.4691, GraphSAGE 0.4724, GAT 0.3910, depth decay 0.3968 — **all four new
heuristic arms outrank all four of those**, and the two best outrank logistic
path features too.

### Paired per-partition PR-AUC, SCP-GNN minus arm

| arm | mean | median | min | max | SCP-GNN ahead in |
|---|---|---|---|---|---|
| H-IR-maxProduct | **+0.0420** | +0.0423 | −0.0103 | +0.0804 | **29/30** |
| H-IR-noisyOR | **+0.0303** | +0.0310 | +0.0019 | +0.0514 | **30/30** |
| H-FIT | +0.0141 | +0.0129 | −0.0143 | +0.0400 | 27/30 |
| H-ORACLE | +0.0094 | +0.0098 | −0.0140 | +0.0290 | 26/30 |

TEST-H (secondary) agrees in sign throughout: +0.0414, +0.0300, +0.0120,
+0.0082.

### Pre-registered rules, and what they returned

| rule | result |
|---|---|
| **R1** — beats both H-IR arms, mean gap > 0 and ≥26/30 | **PASS** (+0.0420 in 29/30; +0.0303 in 30/30) |
| **R2** — beats H-FIT by the same bar | **PASS, narrowly** (+0.0141 in 27/30, bar 26/30) |
| **R3** — H-ORACLE is a diagnostic, not a baseline | honoured: reported, not treated as a loss |
| **R4** — no significance test as a criterion | honoured |
| **R5** — ECE reported, not a gate | honoured |

## The trade-off against H-FIT, in full

Where SCP-GNN's narrow PR-AUC lead comes from and what it costs:

| metric | SCP-GNN | H-FIT | gap | SCP-GNN better in |
|---|---|---|---|---|
| PR-AUC | 0.5519 | 0.5379 | +0.0141 | 27/30 |
| recall | 0.6826 | 0.6255 | +0.0571 | 21/30 |
| F1 | 0.5696 | 0.5524 | +0.0173 | 24/30 |
| **precision** | 0.4977 | **0.5073** | −0.0096 | **12/30** |
| **ECE** | 0.1316 | **0.1204** | +0.0112 worse | **11/30** |

A 1,441-parameter MLP over node and edge features buys **+0.014 PR-AUC** over
an eight-cell lookup table, and gives back precision and calibration to get it.

## What the weights say

The fitted table recovers the generator's rates closely; the hand-authored one
gets the *ordering* right and the *magnitudes* badly wrong.

| cell | H-FIT | oracle (truth) | IncidentRelay |
|---|---|---|---|
| Synchronous / critical | 0.688 | 0.880 | 1.000 |
| Synchronous / non-critical | 0.490 | 0.520 | 0.850 |
| Data / critical | 0.632 | 0.700 | 1.000 |
| Data / non-critical | 0.496 | 0.480 | 0.850 |
| Asynchronous / critical | 0.441 | 0.450 | 0.750 |
| Asynchronous / non-critical | 0.250 | 0.180 | 0.550 |
| Config / critical | 0.399 | 0.400 | 0.200 |
| Config / non-critical | 0.228 | 0.150 | 0.200 |

| versus the oracle | Spearman | MAE | mean weight |
|---|---|---|---|
| H-FIT | **0.976** | **0.058** | 0.453 |
| IncidentRelay | 0.946 | 0.255 | 0.675 (truth: 0.470) |

**The ordering of dependency types is easy to set by hand; the magnitudes are
not.** IncidentRelay's multipliers rank the edge types almost correctly
(ρ = 0.946) while deviating from them by 0.26 on average and running 0.21 too
high in the mean, and that single defect is
what produces ECE 0.43 and an operating point of 0.97 recall at 0.35
precision — close to reachability's behaviour. Fitting the same 9 cells on
training data alone fixes the magnitudes and the calibration together:
ECE 0.46 → 0.12, PR-AUC +0.028.

## Caveats, stated rather than buried

1. **F1, precision and recall are at the fixed 0.5 threshold for every model**,
   which penalises the H-IR arms specifically: their multipliers were never
   intended as probabilities, so almost everything reachable clears 0.5. The
   threshold-free comparison is PR-AUC, which is the primary metric. A
   best-threshold F1 comparison is not possible without retraining SCP-GNN,
   because the frozen artifact does not store `best_f1`.
2. **H-ORACLE is not a baseline.** It is handed the generator's own per-type
   rates. It bounds any fixed table on this substrate and nothing more.
3. **The type→multiplier mapping is ours, not IncidentRelay's.** Its docs give
   six multipliers over a different vocabulary ({hard, soft, external,
   informational} × {required, important, optional}). The mapping — Synchronous
   and Data → hard, Asynchronous → soft, Config → external, `is_critical` →
   required/important — is recorded in `ml/heuristic.py` so a reviewer can
   disagree with it precisely. Two Config cells are interpolated from
   external/optional = 0.20 and are marked as such.
4. **Still one graph, still simulated cascades.** Claims C2, C3 and C4 in
   [00-CLAIMS-STATUS.md](00-CLAIMS-STATUS.md) apply unchanged. 30 partitions of
   one 708-service graph share services; no interval here has valid coverage,
   and no significance test was used.
5. **This establishes nothing about novelty.** Both halves are prior art —
   SCP-GNN is hyperparametric IC (Kalimeris 2018, Xiang 2024); the heuristic is
   IncidentRelay's documented scheme. The result is a measurement, not a
   contribution to method.

## What it does and does not change

**Supports:** that learned per-edge transmission beats the documented
hand-authored multiplier scheme on this substrate, consistently
(+0.0420 PR-AUC, 29/30 partitions) — the specific claim 07's audit said we
could not make.

**Weakens:** the apparent size of SCP-GNN's advantage. The strongest baseline
is no longer logistic path features at +0.0452; it is an eight-cell table at
+0.0141, which also beats SCP-GNN on precision and calibration. Any statement
that SCP-GNN "wins 29 or 30 of 30 against every baseline" must now be read
against this arm.

**Corroborates:** the parsimony result recorded as preliminary claim B1 — a
28-parameter linear link matching the 1,441-parameter MLP. An eight-cell table
coming within 0.014 PR-AUC points the same way, this time under the corrected
30-partition protocol and with a stored artifact.

**If asked "so why not ship the table?"** — it is a defensible answer. It is
interpretable, 9 numbers, better calibrated, and within 0.014 PR-AUC. The
honest case for SCP-GNN is recall (+0.057) and F1 (+0.017) at equal
information, not a decisive margin.

---

### Reproduce

```bash
python -m pytest ml/tests/test_heuristic.py -q          # 17 tests
python -m ml.experiments.run_fixed_weight --partitions 32   # 7.1 min
```

Artifacts: `ml/experiments/results/fixed_weight_baseline.json` (+ `.log`).
The script refuses to start if that file already exists, and halts if the
regenerated data differs from the frozen run by more than 1e-12.
