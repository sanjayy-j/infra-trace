# Methods

## 1. Graph construction

The system of record is a normalised MySQL schema (10 tables, 15 foreign keys,
14 CHECK constraints). The dependency graph is the `dependency` table: a row
`(component_id = u, depends_on_id = v)` reads *u depends on v*.

**Direction convention, applied once.** Failure propagates from a dependency to
its dependents, so a dependency edge `u → v` becomes a propagation edge
`v → u`. The reversal happens in exactly one place (`ml/graph.from_edges`) and
matches the reverse traversal in `database/queries/07_blast_radius.sql`. A test
asserts the Python traversal agrees with the stored function
`fn_blast_radius_count` on every component.

**Features.**

| | source | contents |
|---|---|---|
| node (11 dims) | `component` | `component_type` one-hot (7), `criticality` ordinal, `is_active`, normalised in/out degree |
| edge (5 dims) | `dependency` | `dependency_type` one-hot (4), `is_critical` |

Degrees are `log1p`-compressed and normalised by subgraph size so a model
trained on small graphs transfers to larger ones. **No feature is derived from
incident history** — using `fn_incident_count` or `fn_risk_score` would leak the
label, since both read the incident tables.

## 2. Evaluation topology

The substrate is the **ChronoGraph** dependency graph: 708 production services,
1,529 directed edges, from Bitdefender (Lutu et al. 2025, arXiv:2509.04449,
Apache-2.0). Only `edges.csv` (33 KB) is used; the metric time series and
anomaly labels are a 1.79 GB Git-LFS object and are not used.

Component types and criticality are **not published by ChronoGraph** and are
synthesised: type from structural role inside each subgraph (nothing depends on
it → gateway-like; it depends on nothing → datastore-like), criticality sampled
from InfraTrace's marginal **independently of degree**, deliberately, so the
attribute cannot manufacture a signal that predicts blast radius.

## 3. Labels and cascade simulation

Cascades are **simulated**, not observed. The ground-truth process is
deliberately *not* a pure Independent Cascade, so the model is not fitted to
data drawn from its own hypothesis class. Each node receives one of three
mechanisms:

- **OR** — fails if any incoming dependency transmits (IC-representable);
- **QUORUM** — fails only when ≥k dependencies have failed (a threshold rule
  noisy-OR cannot express; 8.9% of nodes in practice, because nodes with
  in-degree < 2 are demoted to OR);
- **RESILIENT** — retries/circuit breakers damp every incoming probability.

Labels then pass through observation noise (5% false negative, 2% false
positive) because real postmortems miss things. Base transmission rates are
informed by rates measured on the 22 real incidents (synchronous+critical 85.7%,
synchronous 50%, data 50%, asynchronous 40%) and jittered ±15% per graph.

## 4. Partition protocol

The 708 services are partitioned into **service-disjoint** train/val/test pools
(0.50/0.20/0.30), hub-stratified so every pool receives hubs from every
in-degree tier. Per-tier quotas use **largest-remainder allocation** so they sum
exactly to the tier size (T1 11 → 6/2/3; T2 19 → 9/4/6; T3 79 → 39/16/24).

- Hubs are dealt by an interleaving that rotates with the partition seed, so a
  different hub is held out each time. The global maximum in-degree (73) appears
  in the test pool in roughly 30% of partitions.
- A hub keeps every dependent not already claimed by a larger hub; edges
  crossing a pool boundary are **dropped and counted, never duplicated**. Mean
  hub retention in test is 65%; per-hub retention is reported.
- A structural feasibility gate rejects a partition whose pool sizes deviate by
  more than 7 percentage points. Rejection depends only on the graph and seed —
  never on a model output — so it cannot bias the comparison.

**32 partitions accepted** from seeds 0–33 (seeds 31, 32 rejected by the gate);
**2 further partitions (11, 25) proved unusable** because their validation pool
could not yield a single connected subgraph of ≥40 services with ≥25 edges.
**30 partitions were analysed.**

Within each pool, 60 train / 18 val / 32 test connected subgraphs of 40–120
services are sampled, 6 cascades each. Two test sets are produced and **never
pooled**: **TEST-R** (uniform seeds, primary) and **TEST-H** (hub-seeded,
secondary diagnostic).

## 5. Models and baselines

All neural baselines receive identical inputs — node features, root indicator
and edge features concatenated into each message — so a measured gain cannot be
explained by information access. Hyperparameters are fixed at hidden 32, 4
rounds/layers, 120 epochs for every model; nothing was tuned.

| model | description |
|---|---|
| Reachability (SQL) | the deployed recursive CTE: 1.0 for every reachable component |
| Depth decay | λ^depth, one parameter fitted on train |
| Logistic (path features) | reachability, depth, 1/depth, log route count, strongest incoming edge, node features |
| GCN / GraphSAGE / GAT | conventional message passing, 4 layers |
| **SCP-GNN** | `p_e = σ(MLP([x_s; x_t; z_e]))` composed by a noisy-OR fixed point in log-survival space |

SCP-GNN's propagation is a fixed-point iteration, not a stack of layers: the
same transmission probabilities are reused each round, so depth costs no
parameters. Training is weighted BCE (`pos_weight` = negatives/positives,
≈2.18) over the reachable-only mask, Adam lr 0.01, early stopping on validation
PR-AUC.

## 6. Metrics and aggregation

PR-AUC (primary — threshold-free and insensitive to the large negative class),
precision, recall, F1 at a fixed 0.5 threshold, and ECE with 10 equal-width bins.

**Aggregation, stated explicitly:** decisions are pooled within a partition
(micro-average), the 3 initialisations are averaged within the partition, and
the 30 partition values are averaged unweighted. **The experimental unit is the
partition**; subgraph draws, cascade draws and initialisations are nested
replication and never enter degrees of freedom.

## 7. Leakage controls

- Service pools are pairwise disjoint; a test service never appears in training.
- Features are computed on the **induced** subgraph only — a test asserts that
  adding arbitrary edges with one endpoint outside a node set leaves every
  feature array bitwise identical.
- Early stopping uses validation only; the test set is touched once, after
  training.
- Hyperparameters and the 0.5 threshold were fixed in advance and never tuned
  on test results.
