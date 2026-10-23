# Likely examiner questions, with evidence-backed answers

Answers are short on purpose. Every number is verifiable from the artifacts.

---

### 1. "What's novel here?"

**No algorithmic novelty, and we don't claim any.** SCP-GNN is the
hyperparametric Independent Cascade model (Kalimeris et al. 2018), applied to
cascading failures by Xiang et al. 2024 (arXiv:2406.08522). We found that
*after* building it, documented it in `docs/ml/novelty.md`, and withdrew the
claim. A later system-level audit against IncidentRelay and Li (2026) withdrew
the system-level claim too (`07-NOVELTY-AUDIT.md`).

This is an **applied, system-level and empirical** contribution: the database
integration, a **controlled comparison of learned propagation against the
practice it would replace** (deterministic reachability, hand-authored
dependency weights, a compact fitted heuristic) on identical partitions and
metric code, an evaluation on real production topology that overturned our own
earlier conclusion, a measured 78.4% recall ceiling, and four pre-registered
negative results. Of the prior work we reviewed, none evaluates learned
edge-transmission probabilities against the hand-authored scheme that is
actually deployed, and none reports the calibration cost — that is the gap in
the work reviewed, not a claim about every system in existence.

### 2. "Then why is this a project and not just an application?"

Because the evaluation produced findings the application alone would not. We
showed our own earlier result — that logistic path features match the
propagation model — was an artifact of synthetic graphs with in-degree Gini
0.513 against the real 0.781. On a corrected substrate the conclusion reverses.
Knowing an evaluation was wrong is a result.

### 3. "Does it generalise to other systems?"

**No, and we can't claim it does.** All 30 partitions re-partition one
708-service graph from one organisation. They share services, so they are not
independent. Partition-level consistency (29/30) shows robustness to *how we
split the graph*, not to a different company's system.

### 4. "Are these results statistically significant?"

We deliberately **don't make significance claims**. Partitions share services,
so no interval here has valid coverage, and any p-value would be
anti-conservative by an unknown factor. Decisions were made on effect size and
sign consistency against pre-registered thresholds — which the study then
failed on two of six checks.

### 5. "It failed its own criteria — why present it?"

Because reporting it is the honest option. It failed on minimum partition gap
(−0.0122 against a >−0.01 rule) and ECE regression (+0.0938 against ≤0.02). We
set those thresholds before running and did not relax them afterwards. The
effect itself is consistent — 29/30 partitions — which is why it's exploratory
evidence rather than nothing.

### 6. "Your model is worse calibrated than logistic regression. Isn't that fatal for a model that outputs probabilities?"

It's the most serious weakness, and we investigated it rather than ignoring it.
ECE 0.132 against 0.038. Two fixes were pre-registered and both failed their
guards: isotonic calibration gives ECE 0.043 but costs 0.137 F1; removing
`pos_weight` gives 0.039 but costs 0.119 F1 in 30 of 30 partitions. The
miscalibration is monotone overconfidence consistent with weighted BCE —
`p* = wq/(wq+1−q)` at w=2.18 predicts the observed reliability curve with
correlation 0.994. We left the default unchanged because the guard failed.

### 7. "The cascades are simulated. So what have you actually measured?"

The effect of **real topology**, with everything else held constant. ChronoGraph
publishes a dependency graph but no propagation labels, so cascades are
simulated. We state this everywhere and never describe results as real-world
failure prediction. Our only real propagation evidence is 22 incidents on a
19-node graph, reported separately.

### 8. "If the cascades are your own simulation, aren't you just fitting your own assumptions?"

Partly, and we guarded against it. The generator deliberately includes
mechanisms the model **cannot represent** — quorum (k-of-n) nodes and
retry-amplified forward propagation — so the model is misspecified with respect
to its own training data. We declined to "fix" that mismatch, because removing
the guard to raise a score would be fitting the generator.

### 9. "Why is SCP-GNN better than GCN? It's a simpler model."

Its hidden state **is** the failure probability, composed by a fixed noisy-OR
rule, instead of an abstract embedding read out by a classifier. On hub-heavy
real topology GCN, GraphSAGE and GAT all degrade while SCP-GNN holds up
(+0.083, +0.080, +0.161 PR-AUC). That's consistent with Fountoulakis et al.
(2023) on structure governing when message passing pays — we're observing a
known phenomenon, not discovering one.

### 10. "Can the model flag something the SQL wouldn't?"

**No, provably.** If a component is unreachable from the root, every term in the
noisy-OR product involves a probability of zero, so its output is exactly zero —
not approximately. Proof in `docs/ml/design.md` §4.2; asserted in the test suite
on both random and trained weights. The model can only prune the SQL's false
positives, never invent a component.

### 11. "What's the 78.4% ceiling?"

8 of 37 recorded impacts are unreachable from their root cause by reverse
traversal — a caller damaging what it calls, like a rate limiter rejecting valid
traffic or a retry storm hitting a database. No reverse-traversal method can
exceed 78.4% recall, including the deployed SQL. We built the bidirectional
version; it lifts the ceiling to 97.3% but triples the candidate set and halves
precision, so we rejected it.

### 11b. "Did you compare against the simple heuristic any on-call tool already uses?"

**Yes, and it is the answer we are least comfortable with — so here it is.** A
hand-authored dependency-multiplier table, of the kind IncidentRelay documents
(hard/required 1.00 … informational 0.00), propagated by its own rule, reaches
PR-AUC 0.5099 on the identical TEST-R partitions. We beat it by **+0.0420 in
29 of 30 partitions**, which is the claim this experiment was run to test.

But fitting that table's **eight weights** on training data raises it to
**0.5379**, which makes it the strongest baseline in the study — ahead of
logistic path features, GCN, GraphSAGE and GAT — and our margin over it is only
**+0.0141 (27/30)**. It is also *better calibrated* than SCP-GNN (ECE 0.1204 vs
0.1316) and slightly more precise (0.5073 vs 0.4977).

The informative part is *where* the hand-authored table fails. Its ordering of
dependency types is nearly right (Spearman 0.946 against the generator's true
rates) but its magnitudes are wrong by 0.26 on average and run 0.21 too high
in the mean (0.675 against a true 0.470), and that one defect
produces ECE 0.4251 and an operating point of 0.97 recall at 0.36 precision.
Two thirds of everything learning buys here comes from correcting eight numbers;
the graph-neural model earns the last third and pays for it in calibration.
Details and caveats in `08-FIXED-WEIGHT-BASELINE.md`.

### 12. "Why didn't the linear model become the default?"

That result is **preliminary** and we're labelling it so — though the
eight-cell heuristic above now points the same way under the corrected
protocol, with a stored artifact. The 28-parameter
linear link matching the 1,441-parameter MLP was measured in an inline run with
no saved artifact and no script, on the older substrate — not the corrected
30-partition protocol. It's not reproducible from the repository, so it isn't a
headline claim.

### 13. "What would you do with more time?"

Get a second organisation's dependency graph. Every limitation traces back to
having one. Not a better model — the binding constraint is data, not
architecture.

### 14. "What's the database contribution, as opposed to the ML?"

The SQL remains the system of record. Blast radius is defined once, in a
recursive CTE, and a test asserts the Python traversal agrees with
`fn_blast_radius_count` on every component. The API returns structure
(`reachable`, `hops_away`) and estimate (`predicted_probability`) as separate
fields, so a consumer who distrusts the model gets exactly the pre-existing
behaviour. 166 database tests, 45 API tests.

### 15. "Which of your four negative results was most informative?"

Missing-edge recovery. It failed for an arithmetic reason that generalises:
candidate pairs grow O(N²) while missing edges grow O(N), so the link-prediction
base rate is 2.1%. The scorer reached ROC-AUC 0.87 and still only PR-AUC 0.079 —
it recovered 7 of 70 hidden impacts while inventing 23 false positives and
losing 15 true ones. No architecture fixes a base rate.
