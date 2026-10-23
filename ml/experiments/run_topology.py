"""
Does the "logistic ties the GNN" conclusion survive on REAL topology?

THE QUESTION
------------
docs/ml/experiments.md reports logistic regression on hand-engineered path
features tying SCP-GNN on ranking (+0.003 PR-AUC, p = 0.46), and reads that as
"message passing is not what produces good ranking here". That was measured
entirely on ml/synth.py graphs, whose degree distribution is far more
homogeneous than a real system's: in-degree Gini 0.51 against ChronoGraph's
0.78, maximum in-degree 4 against 73.

Fountoulakis et al. (2023, arXiv:2310.07667) characterise GNN performance
against random-graph structure and find that heavy-tailed degree distributions
enhance GNN performance, and that GCNs provably cannot beat linear models
under some conditions. If that applies here, the tie is an artefact of the
substrate and should not survive on real structure.

    H0: the SCP-GNN - logistic gap is the same on both substrates.
    H1: the gap is larger on real topology.

WHAT IS HELD CONSTANT
---------------------
Everything except topology. Same models, same training loop, same cascade
process (ml/synth.simulate_cascade), same features, same metrics, same seeds.
The only difference between the two arms is where the graphs come from, so a
difference in the gap is attributable to topology alone.

WHAT THIS IS NOT
----------------
Not real-world validation of failure propagation. ChronoGraph publishes no
propagation labels, so cascades here are simulated on real structure. The only
real propagation evidence in this project remains the 22 InfraTrace incidents.

LEAKAGE
-------
Real topologies are drawn from BFS-contiguous, node-disjoint train/val/test
pools of the 708-service graph (ml/realtopo.split_node_pools). No service in
training can appear in test. Subgraphs may overlap WITHIN a pool, which is
augmentation, not leakage.

Run:
    python -m ml.experiments.run_topology --seeds 10
    python -m ml.experiments.run_topology --seeds 3 --quick
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from scipy import stats

from ..evaluate import aggregate, summarize
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
from ..realistic import gini
from ..realtopo import RealTopoConfig, generate_real_topology_dataset, split_node_pools
from ..synth import generate_dataset
from ..train import (
    TrainConfig,
    collect_predictions,
    collect_predictions_classical,
    train_model,
)

RESULTS = Path(__file__).resolve().parent / "results"
REAL_CFG = RealTopoConfig(nodes_per_subgraph=(20, 40), min_edges=12)


def synthetic_splits(seed: int, quick: bool):
    a, b, c = (20, 8, 14) if quick else (60, 18, 32)
    return (
        generate_dataset(seed * 1000 + 1, a, 6),
        generate_dataset(seed * 1000 + 2, b, 6),
        generate_dataset(seed * 1000 + 3, c, 6),
    )


def real_splits(seed: int, quick: bool):
    a, b, c = (20, 8, 14) if quick else (60, 18, 32)
    tr_pool, va_pool, te_pool = split_node_pools(0, fractions=(0.55, 0.20, 0.25))
    return (
        generate_real_topology_dataset(seed * 1000 + 1, a, 6, REAL_CFG,
                                       node_pool=tr_pool, disjoint=False),
        generate_real_topology_dataset(seed * 1000 + 2, b, 6, REAL_CFG,
                                       node_pool=va_pool, disjoint=False),
        generate_real_topology_dataset(seed * 1000 + 3, c, 6, REAL_CFG,
                                       node_pool=te_pool, disjoint=False),
    )


def describe(ds) -> dict:
    g_in, epn, pos, tot = [], [], 0, 0
    for graph, cascades in ds:
        d = np.zeros(graph.num_nodes)
        for e in range(graph.num_edges):
            d[int(graph.src[e])] += 1      # src is the dependency: who depends on me
        g_in.append(gini(d))
        epn.append(graph.num_edges / max(graph.num_nodes, 1))
        for cas in cascades:
            m = cas.eval_mask(graph)
            tot += int(m.sum())
            pos += int(cas.labels[m].sum())
    return {
        "topologies": len(ds),
        "mean_nodes": float(np.mean([g.num_nodes for g, _ in ds])) if ds else 0.0,
        "in_degree_gini": float(np.mean(g_in)) if g_in else 0.0,
        "edges_per_node": float(np.mean(epn)) if epn else 0.0,
        "positive_rate": pos / max(tot, 1),
    }


def run_models(train_ds, val_ds, test_ds, seed: int, epochs: int) -> dict[str, dict]:
    out: dict[str, dict] = {}

    y, p = collect_predictions_classical(ReachabilityBaseline(), test_ds)
    out["Reachability (SQL)"] = summarize(y, p)

    y, p = collect_predictions_classical(DepthDecayBaseline().fit(train_ds), test_ds)
    out["Depth decay"] = summarize(y, p)

    y, p = collect_predictions_classical(LogisticBaseline().fit(train_ds), test_ds)
    out["Logistic (path feats)"] = summarize(y, p)

    for name, ctor in (
        ("GCN", lambda: GCNNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        ("GraphSAGE", lambda: SAGENet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        ("GAT", lambda: GATNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        ("SCP-GNN", lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4))),
    ):
        torch.manual_seed(seed)
        model = ctor()
        train_model(model, train_ds, val_ds, TrainConfig(epochs=epochs, seed=seed))
        y, p = collect_predictions(model, test_ds)
        out[name] = summarize(y, p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    substrates = {"SYNTHETIC topology": synthetic_splits, "REAL topology": real_splits}
    per_seed: dict[str, dict[str, list[dict]]] = {k: {} for k in substrates}
    shape: dict[str, dict] = {}

    for sub_name, make in substrates.items():
        for seed in range(args.seeds):
            print(f"--- {sub_name} seed {seed} ---", flush=True)
            tr, va, te = make(seed, args.quick)
            if not tr or not va or not te:
                print("    empty split, skipped")
                continue
            if seed == 0:
                shape[sub_name] = {"train": describe(tr), "test": describe(te)}
            for name, res in run_models(tr, va, te, seed, args.epochs).items():
                per_seed[sub_name].setdefault(name, []).append(res)

    print()
    print("Substrate shape (test split, seed 0)")
    print(f"{'':22}{'topologies':>12}{'nodes':>8}{'in-Gini':>10}{'edges/node':>12}{'pos rate':>10}")
    print("-" * 74)
    print(f"{'REAL full graph':22}{1:>12}{708:>8}{0.781:>10.3f}{2.16:>12.2f}{'-':>10}")
    for sub_name in substrates:
        d = shape.get(sub_name, {}).get("test", {})
        if d:
            print(f"{sub_name:22}{d['topologies']:>12}{d['mean_nodes']:>8.0f}"
                  f"{d['in_degree_gini']:>10.3f}{d['edges_per_node']:>12.2f}"
                  f"{d['positive_rate']:>10.3f}")

    order = ["Reachability (SQL)", "Depth decay", "Logistic (path feats)",
             "GCN", "GraphSAGE", "GAT", "SCP-GNN"]
    for sub_name in substrates:
        print(f"\n=== {sub_name}, {args.seeds} seeds ===")
        print(f"{'model':26}{'PR-AUC':>18}{'precision':>11}{'recall':>9}{'F1':>18}")
        print("-" * 82)
        for name in order:
            runs = per_seed[sub_name].get(name)
            if not runs:
                continue
            a = aggregate(runs)
            print(f"{name:26}{a['pr_auc_mean']:>12.3f}+-{a['pr_auc_std']:.3f}"
                  f"{a['precision_mean']:>11.3f}{a['recall_mean']:>9.3f}"
                  f"{a['f1_mean']:>12.3f}+-{a['f1_std']:.3f}")

    # --- the hypothesis test --------------------------------------------
    print("\n\nH1: the SCP-GNN - logistic gap is larger on real topology")
    print(f"{'substrate':22}{'metric':9}{'gap':>10}{'95% CI':>22}{'p':>9}{'wins':>8}")
    print("-" * 82)
    gaps: dict[str, dict[str, np.ndarray]] = {}
    for sub_name in substrates:
        gaps[sub_name] = {}
        for metric in ("pr_auc", "f1"):
            a = np.array([r[metric] for r in per_seed[sub_name].get("SCP-GNN", [])])
            b = np.array([r[metric] for r in per_seed[sub_name].get("Logistic (path feats)", [])])
            if a.size < 2 or b.size != a.size:
                continue
            d = a - b
            gaps[sub_name][metric] = d
            se = d.std(ddof=1) / np.sqrt(d.size)
            ci = stats.t.interval(0.95, d.size - 1, loc=d.mean(), scale=se)
            p = stats.ttest_rel(a, b).pvalue
            mark = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else " ns"
            print(f"{sub_name:22}{metric:9}{d.mean():>+10.4f} [{ci[0]:>+.4f},{ci[1]:>+.4f}]"
                  f"{p:>9.4f}{mark}{int((d > 0).sum()):>5d}/{d.size}")

    print()
    for metric in ("pr_auc", "f1"):
        if all(metric in gaps[s] for s in substrates):
            a, b = gaps["REAL topology"][metric], gaps["SYNTHETIC topology"][metric]
            k = min(a.size, b.size)
            diff = a[:k] - b[:k]
            p = stats.ttest_ind(a, b, equal_var=False).pvalue
            print(f"  gap(real) - gap(synthetic) on {metric}: {a.mean() - b.mean():+.4f}  "
                  f"Welch p={p:.4f}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "topology.json"
    path.write_text(
        json.dumps(
            {"seeds": args.seeds, "shape": shape,
             "summary": {s: {k: aggregate(v) for k, v in per_seed[s].items()} for s in per_seed},
             "per_seed": per_seed},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
