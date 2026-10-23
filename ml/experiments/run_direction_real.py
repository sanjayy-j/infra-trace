"""
Directionality on the 22 real incidents.

This is the only evidence in the whole study that is not self-generated. The
forward-propagation phenomenon was measured here (31.8% against 61.9% for
reverse edges), so this is where a bidirectional model has to show it was
worth building.

READ THE SAMPLE SIZE FIRST
-------------------------
22 incidents. Of the 37 recorded impacts, 7 are reachable only by following a
dependency edge forwards, and 1 is unreachable in either direction because the
graph is missing an edge. Those 7 are the entire upside being measured, so a
single mislabelled component moves the result visibly. Confidence intervals
bootstrap whole incidents and are wide.

SCORING
-------
Every model is scored on the bidirectional candidate set, so reverse-only
models take the recall penalty for what they cannot reach. That penalty is
real: it is the reason the experiment exists.

Run:
    python -m ml.experiments.run_direction_real --seeds 5
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from ..evaluate import summarize
from ..extract import load_graph, load_incident_cascades
from ..graph import BI_EDGE_FEATURE_DIM, EDGE_FEATURE_DIM, NODE_FEATURE_DIM, Cascade, Graph
from ..models import (
    GCNNet,
    LogisticBaseline,
    ReachabilityBaseline,
    SCPConfig,
    SCPNet,
)
from ..models.baselines import BidirectionalReachabilityBaseline
from ..models.scp_bid import SCPBiDConfig, SCPBiDNet, bidirectional_tensors
from ..synth import SynthConfig, generate_dataset
from ..train import TrainConfig, to_tensors, train_model

RESULTS = Path(__file__).resolve().parent / "results"
FORWARD_CFG = SynthConfig(enable_forward_propagation=True)


def bootstrap_ci(per_scenario, metric: str, n_boot: int = 2000, seed: int = 0):
    """Percentile bootstrap resampling whole incidents, not components."""
    rng = np.random.default_rng(seed)
    point = summarize(
        np.concatenate([y for y, _ in per_scenario]),
        np.concatenate([p for _, p in per_scenario]),
    )[metric]
    vals = []
    idx = np.arange(len(per_scenario))
    for _ in range(n_boot):
        pick = rng.choice(idx, size=idx.size, replace=True)
        v = summarize(
            np.concatenate([per_scenario[i][0] for i in pick]),
            np.concatenate([per_scenario[i][1] for i in pick]),
        )[metric]
        if np.isfinite(v):
            vals.append(v)
    if not vals:
        return point, float("nan"), float("nan")
    return point, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def _scenarios_torch(model, graph: Graph, cascades: list[Cascade], bi_edges: bool):
    t = bidirectional_tensors(graph) if bi_edges else to_tensors(graph)
    out = []
    for cas in cascades:
        mask = cas.eval_mask(graph, bidirectional=True)
        if not mask.any():
            continue
        with torch.no_grad():
            q = model(
                t["x"], t["src"], t["dst"], t["edge_attr"],
                torch.tensor([cas.root_index], dtype=torch.long),
            )[0]
        out.append((cas.labels[mask], q.numpy()[mask]))
    return out


def _scenarios_classical(model, graph: Graph, cascades: list[Cascade]):
    out = []
    for cas in cascades:
        mask = cas.eval_mask(graph, bidirectional=True)
        if not mask.any():
            continue
        out.append((cas.labels[mask], model.predict(graph, cas.root_index)[mask]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=120)
    args = ap.parse_args()

    graph = load_graph()
    cascades = load_incident_cascades(graph)
    decisions = sum(int(c.eval_mask(graph, bidirectional=True).sum()) for c in cascades)
    rev_decisions = sum(int(c.eval_mask(graph).sum()) for c in cascades)
    positives = sum(
        int(c.labels[c.eval_mask(graph, bidirectional=True)].sum()) for c in cascades
    )
    print(
        f"Real graph N={graph.num_nodes} E={graph.num_edges}; {len(cascades)} incidents\n"
        f"  candidate set: {rev_decisions} reverse-only -> {decisions} bidirectional, "
        f"{positives} positive"
    )

    train_ds = generate_dataset(777, 70, 6, FORWARD_CFG)
    val_ds = generate_dataset(778, 20, 6, FORWARD_CFG)

    rows: dict[str, list] = {}
    rows["Reachability reverse (SQL today)"] = _scenarios_classical(
        ReachabilityBaseline(), graph, cascades)
    rows["Reachability bidirectional"] = _scenarios_classical(
        BidirectionalReachabilityBaseline(), graph, cascades)
    for name, bi in (("Logistic reverse feats", False), ("Logistic bidirectional feats", True)):
        rows[name] = _scenarios_classical(
            LogisticBaseline(bidirectional=bi).fit(train_ds), graph, cascades)

    neural = [
        ("GCN (bidirectional edges)",
         lambda: GCNNet(NODE_FEATURE_DIM, BI_EDGE_FEATURE_DIM, 32, 4), True),
        ("SCP-GNN (reverse only)",
         lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)), False),
        ("SCP-BiD (proposed)",
         lambda: SCPBiDNet(SCPBiDConfig(NODE_FEATURE_DIM, hidden=32, rounds=4)), True),
        ("  ablation: BiD direction-blind",
         lambda: SCPBiDNet(
             SCPBiDConfig(NODE_FEATURE_DIM, hidden=32, rounds=4, use_direction_flag=False)),
         True),
    ]
    for name, ctor, bi_edges in neural:
        # Average over seeds: one synthetic training run is a noisy draw and
        # reporting a single one would be cherry-picking.
        stacked = []
        for seed in range(args.seeds):
            torch.manual_seed(seed)
            model = ctor()
            train_model(model, train_ds, val_ds, TrainConfig(
                epochs=args.epochs, seed=seed,
                bidirectional_mask=True, bidirectional_edges=bi_edges))
            model.eval()
            stacked.append([p for _, p in _scenarios_torch(model, graph, cascades, bi_edges)])
        ys = [y for y, _ in _scenarios_torch(model, graph, cascades, bi_edges)]
        rows[name] = [
            (ys[i], np.mean([s[i] for s in stacked], axis=0)) for i in range(len(ys))
        ]

    print()
    print(f"Real incidents (n={len(cascades)}), scored on the BIDIRECTIONAL candidate set")
    print("95% bootstrap CI over incidents, 2000 resamples")
    print()
    header = (f"{'model':34}{'PR-AUC (95% CI)':>26}{'prec@.5':>9}"
              f"{'rec@.5':>8}{'F1@.5':>8}{'bestF1':>8}")
    print(header)
    print("-" * len(header))

    out: dict[str, dict] = {}
    for name, scen in rows.items():
        y = np.concatenate([a for a, _ in scen])
        p = np.concatenate([b for _, b in scen])
        m = summarize(y, p)
        point, lo, hi = bootstrap_ci(scen, "pr_auc")
        ci = f"{point:.3f} [{lo:.3f}, {hi:.3f}]" if np.isfinite(lo) else f"{point:.3f}"
        print(f"{name:34}{ci:>26}{m['precision']:>9.3f}"
              f"{m['recall']:>8.3f}{m['f1']:>8.3f}{m['best_f1']:>8.3f}")
        out[name] = {**{k: v for k, v in m.items() if isinstance(v, (int, float))},
                     "pr_auc_ci_low": lo, "pr_auc_ci_high": hi}

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "direction_real.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
