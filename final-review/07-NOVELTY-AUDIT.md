# System-level novelty audit

**Question asked:** are any of four candidate *system* distinctions novel against
named prior art, once the primary sources are read?

**Verdict: none of the four is novel.** Each is documented, by name, in at least
one prior system — and three of the four are documented in a single one
(IncidentRelay). The recommendation is therefore **not** to advance a novelty
claim at system level. `00-CLAIMS-STATUS.md` C1 already forbids algorithmic
novelty; this audit extends the same prohibition to the system framing, which
had not been checked before.

---

## Sources read (primary, not snippets)

| source | what it is | date |
|---|---|---|
| **IncidentRelay** — `README.md`, `docs/concepts/services.md` (roxy-wi/IncidentRelay, ELv2) | self-hosted incident-response platform with a service catalog, "Service Impact v2" and "Blast radius" | v2.2, read 2026-10-16 |
| **Li (2026)**, *Service dependency modeling and failure propagation prediction in distributed systems based on graph neural networks*, Discover Artificial Intelligence, doi:10.1007/s44163-026-01213-3 | GNN framework: telemetry → graph → attention/temporal GNN → stochastic cascade prediction | 2026-04-22 |
| **Xiang et al. (2024)**, arXiv:2406.08522 | hyperparametric diffusion for cascading failures (power grids) | 2024 |
| **Kalimeris et al. (2018)** | hyperparametric Independent Cascade; `p_uv = σ(θᵀx_uv)` | 2018 |

Kalimeris and Xiang were already audited in `docs/ml/novelty.md`; nothing here
changes that reading. IncidentRelay and Li (2026) are new to this audit.

---

## The four candidates, as actually implemented

All four were confirmed in code before being audited — not inferred from the
write-up:

1. **Relational source of truth** — 10 tables in `database/schema/`
   (`team, developer, application, component, application_component, dependency,
   environment, deployment, incident, incident_component`).
2. **SQL blast radius + learned ranking on the same model** —
   `sp_get_blast_radius` (`database/procedures/procedures.sql:88`), recursive CTE
   in `database/functions/functions.sql:82`; ranking in `ml/predict.py:92`.
3. **Edge-level explanations** — `EdgeExplanation` (`ml/predict.py:58`),
   `explain()` (`ml/predict.py:127`), field `transmission_probability`.
4. **API surfaces fact and estimate separately** —
   `backend/app/ml_api.py:115` (`reachable` vs `predicted_probability`),
   `:162` (`edge_transmission`).

---

## Comparison matrix

| # | candidate | closest prior art | documented overlap (evidence) | difference that survives | classification |
|---|---|---|---|---|---|
| 1 | Relational catalog as source of truth for the dependency graph | **IncidentRelay** | README: "SQLite for small single-node installations and PostgreSQL for larger deployments"; `manage.py migrate`; `app/check_schema.py` verifies "the configured database matches the Peewee models". The services doc lists services with owning team, type, environment, criticality, tier; dependencies; incidents; maintenance windows. Commercial CMDBs (ServiceNow and others) are the same pattern at scale. | Our schema is hand-normalised with 15 FKs and 14 CHECK constraints, and the DBMS work is the assessed artifact. A difference in *emphasis*, not capability. | **implementation detail** |
| 2 | Blast radius defined once in SQL, reused by the model | **IncidentRelay** | Services doc, "Blast radius": "Which downstream services can be affected if this service is unhealthy?", returning "direct downstream count; transitive downstream count; critical downstream count; tier 1 downstream count; optional paths; cycle/depth flags". That is transitive closure over a stored dependency table, with cycle handling. | IncidentRelay's docs do **not** state whether the traversal runs in SQL or in application code, so no claim is made either way. Ours is a recursive CTE, and a test asserts the Python traversal agrees with `fn_blast_radius_count` on every component. Single-definition discipline is good engineering, not a new capability. | **implementation detail** |
| 3 | Per-edge transmission probability as the explanation | **IncidentRelay** (deterministic); **Li 2026 Eq. 21–22**; **Kalimeris / Xiang** (learned) | IncidentRelay already has typed edges — `dependency_type` ∈ {hard, soft, external, informational}, `criticality` ∈ {required, important, optional} — a **dependency multiplier table** (hard/required 1.00, hard/important 0.85, soft/required 0.75, soft/important 0.55, external/optional 0.20, informational 0.00), propagation by multiplying the upstream score, and per-item `explanation` ("human-readable explanation and dependency paths") plus `root_causes`. Li 2026 Eq. 21 is a noisy-OR over incoming dependencies, `1 − ∏(1 − p_ki·w_ki)`, and Eq. 22 names an explicit "edge-level propagation probability". | IncidentRelay's multipliers are **hand-authored constants**; Li's `w_ki` is a telemetry formula with grid-searched weights (α=0.4, β=0.3, γ=0.3). Ours is learned end-to-end from impact labels. But learning edge probabilities from edge features *is* Kalimeris 2018 / Xiang 2024. Mechanism = prior art; the conjunction = integration. | **implementation detail** |
| 4 | API returns SQL fact and model estimate as separate fields | **IncidentRelay** | `GET /api/services/{service_id}/details` returns "upstream and downstream dependencies" plus a "current Impact v2 block" carrying `own_status`, `dependency_impact_score`, `effective_status`, `primary_reason`, `root_causes`, `explanation`, `blast_radius` — structural fact and derived score side by side in one versioned contract, with Swagger/OpenAPI at `/docs`. | None of substance. Ours separates *deterministic reachability* from a *calibrated probability*; theirs separates reachability from a deterministic score. The contract shape is the same. | **implementation detail** |

### Where the prior art genuinely does not reach

Stated as absences only where a primary source was read and the capability is
not documented in it — not as a general claim about those systems:

- **IncidentRelay** documents no learning component anywhere in its README or
  services doc: no training, no probability, no calibration. Its propagation is
  deterministic score multiplication mapped onto statuses.
- **Li (2026)** mentions no relational store, no SQL, no reachability baseline,
  no calibration metric and no reproducible artifact ("Source code … will be
  released on GitHub following publication"; datasets "available through
  institutional data sharing agreements"). Its reported 94.1% precision /
  92.3% recall is not checkable.
- **Xiang (2024)** learns the graph rather than reading it from a catalog, and
  works on power grids.

These are real gaps in the prior work **reviewed here**, not evidence that no
system anywhere provides these capabilities — and they are gaps in *those*
artifacts, not a contribution of ours: closing them by assembling known parts is
integration.

---

## Recommendation

**Claim no algorithmic or system-level novelty.** At review, answer "what's
novel here?" by naming the contribution type accurately and leading with the
measurement, which none of the sources reviewed above provides:

> InfraTrace combines database-backed service dependency management with
> deterministic blast-radius analysis and probabilistic impact ranking. Its
> empirical contribution is a controlled comparison of learned propagation
> against deterministic reachability, hand-authored dependency weights, and a
> compact fitted heuristic. On 30 partitions of a real production dependency
> topology with simulated cascades, SCP-GNN improves PR-AUC by **0.0420** over
> the documented hand-authored heuristic and by **0.0141** over the fitted
> heuristic, while exhibiting precision and calibration trade-offs. These
> findings quantify the value and limitations of learned propagation in the
> evaluated setting; they do not establish a novel propagation algorithm or
> generalisation to observed production failures.

That is an **applied, system-level and empirical** contribution, not algorithmic
novelty, and should be presented as such. Of the four sources reviewed here,
none evaluates learned edge-transmission probabilities against the
hand-authored multiplier scheme that is actually deployed, and none reports the
calibration cost; Li (2026) in particular reports no cost at all. That is the
gap **in the prior work reviewed** — it is not a claim that no system anywhere
has done this.

**The stronger framing, now measured.** When this audit was written, "we replace
hand-authored dependency multipliers with learned ones" could not be used,
because the heuristic had never been run (unresolved question 1 below). It has
since been run on the identical TEST-R partitions, and the claim is now
supported at +0.0420 in 29/30 partitions — together with the finding that a
fitted eight-cell version of the same heuristic is the strongest baseline in the
study and is better calibrated than SCP-GNN. See
[08-FIXED-WEIGHT-BASELINE.md](08-FIXED-WEIGHT-BASELINE.md).

---

## Unresolved questions (disclose if asked)

1. ~~**The obvious heuristic baseline was never run.**~~ **RESOLVED.** It has
   been run on the identical TEST-R partitions, verified against the frozen
   baselines to 1e-12: SCP-GNN is ahead of the documented max-product scheme by
   +0.0420 (29/30) and of a fitted eight-cell version by +0.0141 (27/30), the
   latter being the strongest baseline in the study and better calibrated than
   SCP-GNN. The claim "learned beats hand-set multipliers" is now supported;
   the claim that SCP-GNN beats *every* baseline comfortably is not. Full result
   and caveats: [08-FIXED-WEIGHT-BASELINE.md](08-FIXED-WEIGHT-BASELINE.md).
2. **IncidentRelay's traversal mechanism** (SQL versus application code) and its
   exact closure semantics were not determined; only its documentation was read,
   not its source.
3. **Li (2026) tables 1–4 were not retrieved** (rendered as images / separate
   table views), so its baseline list is known only from prose. Its claims are
   in any case not reproducible.
4. **Kalimeris et al. still read only through Xiang §2–3**, as
   `docs/ml/novelty.md` already discloses.

---

*This audit was read-only: no code, model, experiment or frozen artifact was
touched. Repo state at audit time — HEAD `55d0199`, 4 modified + 4 untracked
paths, unchanged from the pre-audit checkpoint.*
