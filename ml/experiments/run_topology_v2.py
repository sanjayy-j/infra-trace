"""
Corrected topology study: SCP-GNN vs logistic on hub-stratified partitions.

This replaces ml/experiments/run_topology.py, whose partition was fixed by a
hardcoded seed so that all ten largest hubs landed in training and the test
pool's maximum in-degree was 10.

WHAT IS FROZEN (set before any result was seen; not changed afterwards)
----------------------------------------------------------------------
  partitions        32 feasible, drawn in ascending seed order
  rejection         structural only: max pool-size deviation > 0.07
  subgraphs         60 train / 18 val per partition
  evaluation        TEST-R 32 subgraphs (PRIMARY, uniform seeds)
                    TEST-H 32 subgraphs (SECONDARY, hub-seeded)
                    never pooled, never averaged together
  sizing            nodes_per_subgraph (40, 120), min_edges 25
  cascades          6 per subgraph
  model inits       3 per partition, averaged WITHIN partition
  hyperparameters   hidden 32, rounds 4, epochs 120 - identical for all models
  unit of analysis  the partition

THE UNIT IS THE PARTITION
-------------------------
Subgraph draws, cascade draws and model inits are nested replication. They are
averaged within a partition to produce ONE paired value, and never enter
degrees of freedom. The previous study treated 10 model seeds under a single
fixed partition as independent units, which measured the wrong variance.

WHAT CANNOT BE CLAIMED
----------------------
All 32 partitions re-partition ONE 708-service graph from ONE organisation.
They are not independent systems. No confidence interval here has valid
coverage for a population of graphs, and no significance test is used as a
decision criterion - nominal p-values are printed as flagged descriptives
only. Cascades are SIMULATED on real structure; they are not observed
production failures. Every conclusion is conditional on this graph.

Run:
    python -m ml.experiments.run_topology_v2 --partitions 32
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import torch

from ..evaluate import summarize
from ..graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM
from ..models import (
    DepthDecayBaseline,
    GATNet,
    GCNNet,
    LogisticBaseline,
    ReachabilityBaseline,
    SAGENet,
    SCPConfig,
    SCPNet,
)
from ..partition import (
    POOLS,
    SampleConfig,
    build_graph,
    sample_pool_subgraphs,
    stratified_split_node_pools,
)
from ..realtopo import _truth_for
from ..synth import SynthConfig, simulate_cascade
from ..train import (
    TrainConfig,
    collect_predictions,
    collect_predictions_classical,
    train_model,
)

RESULTS = Path(__file__).resolve().parent / "results"
ROOT = Path(__file__).resolve().parents[2]

CFG = SampleConfig(nodes_per_subgraph=(40, 120), min_edges=25)
SCFG = SynthConfig()
N_TRAIN, N_VAL, N_TEST = 60, 18, 32
CASCADES = 6
EPOCHS = 120
HIDDEN, ROUNDS = 32, 4

PROTECTED = [
    "ml/models/scp.py", "ml/models/scp_bid.py", "ml/models/scp_latent.py",
    "ml/models/baselines.py", "ml/models/__init__.py", "ml/synth.py", "ml/graph.py",
    "ml/train.py", "ml/predict.py", "ml/train_production.py", "ml/realtopo.py",
    "ml/realistic.py", "ml/incomplete.py", "backend/app/ml_api.py",
    "backend/app/main.py", "ml/artifacts/scp_gnn.pt", "ml/artifacts/scp_linear.pt",
]


def hash_protected() -> dict[str, str]:
    out = {}
    for rel in PROTECTED:
        p = ROOT / rel
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "MISSING"
    return out


def build_dataset(pool, seed, count, rng, hub_seeded=False, hub_nodes=None):
    """Sample subgraphs from one pool and simulate cascades on each.

    Samples ONCE and returns both the datasets and the service sets, so
    coverage reporting costs nothing extra.
    """
    samples = sample_pool_subgraphs(
        pool, seed, count, CFG, hub_seeded=hub_seeded, hub_nodes=hub_nodes
    )
    out, keeps = [], []
    for comps, deps, keep in samples:
        graph = build_graph(comps, deps)
        if graph.num_edges == 0:
            continue
        truth = _truth_for(graph, deps, rng, SCFG)
        reach = np.array(
            [graph.reachable_from(i).sum() for i in range(graph.num_nodes)], dtype=float
        )
        if reach.sum() == 0:
            continue
        w = reach / reach.sum()
        cascades = [
            simulate_cascade(rng, graph, truth, int(rng.choice(graph.num_nodes, p=w)), SCFG)
            for _ in range(CASCADES)
        ]
        out.append((graph, cascades))
        keeps.append(keep)
    return out, keeps


def coverage(pool, keeps) -> tuple[int, int, float]:
    """Distinct services covered, pool size, mean pairwise Jaccard."""
    if not keeps:
        return 0, len(pool), 0.0
    cov = len(set().union(*keeps))
    ov = [len(a & b) / len(a | b) for i, a in enumerate(keeps) for b in keeps[i + 1:]]
    return cov, len(pool), float(np.mean(ov)) if ov else 0.0


def evaluate_partition(part, inits: int) -> dict:
    """One partition: train on its pools, score TEST-R and TEST-H separately."""
    rng = np.random.default_rng(part.seed)
    hubs = [h.node for h in part.hubs_in("test")]

    train_ds, _ = build_dataset(part.pools["train"], part.seed * 97 + 1, N_TRAIN, rng)
    val_ds, _ = build_dataset(part.pools["val"], part.seed * 97 + 2, N_VAL, rng)
    test_r, keeps_r = build_dataset(part.pools["test"], part.seed * 97 + 3, N_TEST, rng)
    test_h, keeps_h = build_dataset(
        part.pools["test"], part.seed * 97 + 4, N_TEST, rng,
        hub_seeded=True, hub_nodes=hubs,
    )
    if not (train_ds and val_ds and test_r):
        return {"seed": part.seed, "usable": False}

    scores: dict[str, dict[str, list[float]]] = {}

    def add(name, setname, metrics):
        scores.setdefault(name, {}).setdefault(setname, []).append(metrics)

    # --- non-neural baselines (deterministic; one pass) ---
    for name, model in (
        ("Reachability (SQL)", ReachabilityBaseline()),
        ("Depth decay", DepthDecayBaseline().fit(train_ds)),
        ("Logistic (path feats)", LogisticBaseline().fit(train_ds)),
    ):
        for setname, ds in (("TEST-R", test_r), ("TEST-H", test_h)):
            if not ds:
                continue
            y, p = collect_predictions_classical(model, ds)
            add(name, setname, summarize(y, p))

    # --- neural models: `inits` runs, averaged within the partition ---
    for name, ctor in (
        ("GCN", lambda: GCNNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, HIDDEN, ROUNDS)),
        ("GraphSAGE", lambda: SAGENet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, HIDDEN, ROUNDS)),
        ("GAT", lambda: GATNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, HIDDEN, ROUNDS)),
        ("SCP-GNN", lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, HIDDEN, ROUNDS))),
    ):
        for init in range(inits):
            torch.manual_seed(part.seed * 1000 + init)
            model = ctor()
            train_model(model, train_ds, val_ds, TrainConfig(epochs=EPOCHS, seed=init))
            for setname, ds in (("TEST-R", test_r), ("TEST-H", test_h)):
                if not ds:
                    continue
                y, p = collect_predictions(model, ds)
                add(name, setname, summarize(y, p))

    # average nested replication within the partition
    summary = {
        name: {
            s: {k: float(np.mean([m[k] for m in lst if np.isfinite(m[k])]))
                for k in ("pr_auc", "precision", "recall", "f1", "ece")}
            for s, lst in sets.items()
        }
        for name, sets in scores.items()
    }

    cov_r = coverage(part.pools["test"], keeps_r)
    cov_h = coverage(part.pools["test"], keeps_h)
    test_hubs = part.hubs_in("test")
    retention = float(np.mean([h.retention_fraction for h in test_hubs])) if test_hubs else 0.0

    return {
        "seed": part.seed,
        "usable": True,
        "pool_sizes": {p: int(len(part.pools[p])) for p in POOLS},
        "deviation": {p: float(part.deviation[p]) for p in POOLS},
        "test_hub_count": len(test_hubs),
        "test_max_global_in_degree": max([h.global_in_degree for h in test_hubs], default=0),
        "test_max_retained_in_degree": max([h.retained_in_degree for h in test_hubs], default=0),
        "test_mean_hub_retention": retention,
        "test_hubs": [
            {"node": h.node, "tier": h.tier, "global_in_degree": h.global_in_degree,
             "retained_in_degree": h.retained_in_degree,
             "retention_fraction": round(h.retention_fraction, 4),
             "cut_edges": h.cut_edges,
             "observability_cap": h.observability_cap(CFG.nodes_per_subgraph[1])}
            for h in sorted(test_hubs, key=lambda x: -x.global_in_degree)
        ],
        "coverage": {
            "TEST-R": {"distinct_services": cov_r[0], "pool_size": cov_r[1],
                       "mean_pairwise_jaccard": round(cov_r[2], 4),
                       "subgraphs": len(test_r)},
            "TEST-H": {"distinct_services": cov_h[0], "pool_size": cov_h[1],
                       "mean_pairwise_jaccard": round(cov_h[2], 4),
                       "subgraphs": len(test_h)},
        },
        "metrics": summary,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partitions", type=int, default=32)
    ap.add_argument("--inits", type=int, default=3)
    ap.add_argument("--max-scan", type=int, default=128)
    args = ap.parse_args()

    before = hash_protected()
    started = time.perf_counter()

    print("Drawing partitions under the frozen rules ...", flush=True)
    accepted, rejected = [], []
    p = 0
    while len(accepted) < args.partitions and p < args.max_scan:
        part = stratified_split_node_pools(p)
        (accepted if part.feasible else rejected).append(part if part.feasible else p)
        p += 1
    print(f"  accepted {len(accepted)} partitions (seeds 0..{p-1}); rejected {rejected}")
    if len(accepted) < args.partitions:
        print(f"HALT: only {len(accepted)} feasible partitions in {args.max_scan} seeds.")
        raise SystemExit(1)

    records = []
    for i, part in enumerate(accepted[: args.partitions]):
        t0 = time.perf_counter()
        rec = evaluate_partition(part, args.inits)
        records.append(rec)
        print(f"  [{i+1}/{args.partitions}] seed {part.seed}: "
              f"{'ok' if rec['usable'] else 'UNUSABLE'} "
              f"({time.perf_counter()-t0:.0f}s)", flush=True)

    runtime = time.perf_counter() - started
    after = hash_protected()
    changed = [k for k in before if before[k] != after[k]]

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "topology_v2.json"
    out.write_text(json.dumps({
        "frozen": {"partitions": args.partitions, "inits": args.inits,
                   "subgraphs": {"train": N_TRAIN, "val": N_VAL, "test": N_TEST},
                   "nodes_per_subgraph": list(CFG.nodes_per_subgraph),
                   "min_edges": CFG.min_edges, "cascades": CASCADES,
                   "epochs": EPOCHS, "hidden": HIDDEN, "rounds": ROUNDS},
        "rejected_seeds": rejected,
        "runtime_seconds": runtime,
        "protected_unchanged": not changed,
        "protected_changed": changed,
        "partitions": records,
    }, indent=2), encoding="utf-8")
    print(f"\nRuntime {runtime/60:.1f} min. Wrote {out}")
    print(f"Protected files unchanged: {not changed}" + (f" CHANGED: {changed}" if changed else ""))


if __name__ == "__main__":
    main()
