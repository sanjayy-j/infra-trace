"""
RRD Gate 1: is the route-count calibration gradient an artifact of the
generator's quorum rule?

BACKGROUND
----------
Arm B of the pos_weight ablation (w=1.0, globally well calibrated, ECE 0.0387)
shows a calibration gap that grows with the number of incoming routes:
+0.001 / +0.048 / +0.071 / +0.062 for 1 / 2 / 3 / 4+ routes. That was proposed
as evidence of noisy-OR over-counting correlated paths.

THE CONFOUND, ESTABLISHED BEFORE RUNNING
----------------------------------------
ml/realtopo._truth_for demotes any would-be QUORUM node with in_degree < 2 back
to OR. Measured over 2071 nodes of real test topologies, QUORUM share by
in-degree is 0.0% / 0.0% / 18.7% / 26.0% / 26.3% for in-degree 0 / 1 / 2 / 3 /
4+. A quorum node requires k >= 2 transmissions to fail, so it fails LESS often
than independent noisy-OR predicts - producing a positive gap exactly where
route count is high, and no gap where route count is 1.

So the generator mechanically produces the measured pattern. This control
separates the two explanations.

CONDITIONS
----------
  Q   SynthConfig()                     quorum_fraction = 0.25  (as published)
  NQ  SynthConfig(quorum_fraction=0.0)  quorum disabled -> `needed` is 1 for
                                        every node, i.e. pure OR

Verified: quorum_fraction=0.0 disables the rule rather than trivially
satisfying it - `mechanism[roll < 0.0]` selects nothing, so no node ever takes
the `needed = quorum_k[v]` branch in simulate_cascade.

WHAT IS AND IS NOT PAIRED
-------------------------
PAIRED: partitions, pools, topologies, component features. The subgraph sampler
holds its own rng seeded independently of label generation, so both conditions
see bitwise-identical graphs (verified).

NOT PAIRED: individual cascade draws. The quorum branch consumes rng.integers
once per quorum node, so disabling it shifts every subsequent draw (damping,
jitter, edge_p, live, roots). This cannot be avoided without editing the
protected ml/realtopo.py. Cascades are therefore drawn from the same
distribution on the same graphs, but are not bitwise paired. Reported as a
limitation rather than worked around.

ALSO NOT HELD FIXED: which nodes are RESILIENT. With quorum_fraction=0.25
resilient nodes are roll in [0.25, 0.50); with 0.0 they are roll in [0.0, 0.25).
The FRACTION is identical (0.25) and assignment is random, so the conditions are
distributionally matched, but node identity differs.

Everything else - architecture (32,4), 120 epochs, use_pos_weight=False to match
arm B, early stopping on validation PR-AUC, reachable-only mask, 0.5 threshold,
30 partitions, 3 inits - is unchanged.

Writes results/quorum_control.json only. All historical artifacts untouched.
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
from ..models import SCPConfig, SCPNet
from ..partition import SampleConfig, build_graph, sample_pool_subgraphs, stratified_split_node_pools
from ..realtopo import _truth_for
from ..synth import SynthConfig, simulate_cascade
from ..train import TrainConfig, to_tensors, train_model

RESULTS = Path(__file__).resolve().parent / "results"
ROOT = Path(__file__).resolve().parents[2]

CFG = SampleConfig(nodes_per_subgraph=(40, 120), min_edges=25)
N_TRAIN, N_VAL, N_TEST = 60, 18, 32
CASCADES, EPOCHS, HIDDEN, ROUNDS = 6, 120, 32, 4

CONDITIONS = {
    "Q": SynthConfig(),                       # quorum_fraction = 0.25
    "NQ": SynthConfig(quorum_fraction=0.0),   # quorum disabled
}
ROUTE_BINS = [(1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 10**9, "4+")]
# A bin needs this many decisions before its gap is used in the gradient.
# 30 is the conventional minimum for a proportion estimate; chosen on that
# basis, not tuned to any observed value. A partition whose "1" or "4+" bin
# falls below it has an UNDEFINED gradient and is excluded from the gradient
# analysis - with the exclusion count reported.
MIN_BIN_SUPPORT = 30

PROTECTED = [
    "ml/models/scp.py", "ml/models/scp_bid.py", "ml/models/scp_latent.py",
    "ml/models/baselines.py", "ml/models/__init__.py", "ml/synth.py", "ml/graph.py",
    "ml/train.py", "ml/predict.py", "ml/train_production.py", "ml/realtopo.py",
    "ml/realistic.py", "ml/incomplete.py", "backend/app/ml_api.py",
    "backend/app/main.py", "ml/artifacts/scp_gnn.pt", "ml/artifacts/scp_linear.pt",
]
FROZEN = ["ml/experiments/results/topology_v2.json",
          "ml/experiments/results/calibration_diagnostic.json",
          "ml/experiments/results/posweight_ablation.json",
          "ml/experiments/results/posweight_ablation.npz"]


def hashes(paths):
    return {p: (hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                if (ROOT / p).exists() else "MISSING") for p in paths}


def build_dataset(pool, seed, count, rng, scfg):
    """Datasets plus, for each graph, the mechanism vector (for validity checks)."""
    out, mechs = [], []
    for comps, deps, _k in sample_pool_subgraphs(pool, seed, count, CFG):
        g = build_graph(comps, deps)
        if g.num_edges == 0:
            continue
        truth = _truth_for(g, deps, rng, scfg)
        reach = np.array([g.reachable_from(i).sum() for i in range(g.num_nodes)], dtype=float)
        if reach.sum() == 0:
            continue
        w = reach / reach.sum()
        cas = [simulate_cascade(rng, g, truth, int(rng.choice(g.num_nodes, p=w)), scfg)
               for _ in range(CASCADES)]
        out.append((g, cas))
        mechs.append(truth["mechanism"])
    return out, mechs


@torch.no_grad()
def predict_with_routes(model, dataset):
    """Predictions plus the incoming-route count for every evaluated decision.

    `route` counts incoming edges whose source is the root or reachable from
    it - the same definition used in the audit that produced the gradient.
    Computed from observed topology and the root only; no labels involved.
    """
    model.eval()
    y, p, r = [], [], []
    for graph, cascades in dataset:
        t = to_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        out = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots).cpu().numpy()
        for c_idx, cas in enumerate(cascades):
            m = cas.eval_mask(graph)
            if not m.any():
                continue
            reach = graph.reachable_from(cas.root_index)
            routes = np.zeros(graph.num_nodes)
            for e in range(graph.num_edges):
                a, b = int(graph.src[e]), int(graph.dst[e])
                if a == cas.root_index or reach[a]:
                    routes[b] += 1.0
            y.append(cas.labels[m]); p.append(out[c_idx][m]); r.append(routes[m])
    if not y:
        z = np.array([])
        return z, z, z
    return np.concatenate(y), np.concatenate(p), np.concatenate(r)


def route_table(y, p, r):
    rows = {}
    for lo, hi, lab in ROUTE_BINS:
        s = (r >= lo) & (r <= hi)
        n = int(s.sum())
        rows[lab] = {"n": n,
                     "positives": int(y[s].sum()) if n else 0,
                     "mean_pred": float(p[s].mean()) if n else None,
                     "observed": float(y[s].mean()) if n else None,
                     "gap": float(p[s].mean() - y[s].mean()) if n else None}
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partitions", type=int, default=30)
    ap.add_argument("--inits", type=int, default=3)
    args = ap.parse_args()

    before_p, before_f = hashes(PROTECTED), hashes(FROZEN)
    started = time.perf_counter()

    accepted, p = [], 0
    while len(accepted) < 32 and p < 128:
        part = stratified_split_node_pools(p)
        if part.feasible:
            accepted.append(part)
        p += 1

    print(f"conditions: Q quorum={CONDITIONS['Q'].quorum_fraction}, "
          f"NQ quorum={CONDITIONS['NQ'].quorum_fraction}; "
          f"use_pos_weight=False (matches ablation arm B)")
    records = {c: [] for c in CONDITIONS}

    for cond, scfg in CONDITIONS.items():
        print(f"\n=== CONDITION {cond} (quorum_fraction={scfg.quorum_fraction}) ===", flush=True)
        done = 0
        for part in accepted:
            if done >= args.partitions:
                break
            t0 = time.perf_counter()
            rng = np.random.default_rng(part.seed)
            tr, _ = build_dataset(part.pools["train"], part.seed * 97 + 1, N_TRAIN, rng, scfg)
            va, _ = build_dataset(part.pools["val"], part.seed * 97 + 2, N_VAL, rng, scfg)
            te, te_mech = build_dataset(part.pools["test"], part.seed * 97 + 3, N_TEST, rng, scfg)
            if not (tr and va and te):
                print(f"  seed {part.seed}: UNUSABLE", flush=True)
                continue

            quorum_nodes = int(sum(int((m == 1).sum()) for m in te_mech))
            total_nodes = int(sum(m.size for m in te_mech))

            per_init = []
            for init in range(args.inits):
                torch.manual_seed(part.seed * 1000 + init)
                model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, HIDDEN, ROUNDS))
                train_model(model, tr, va,
                            TrainConfig(epochs=EPOCHS, seed=init, use_pos_weight=False))
                y, pr, rt = predict_with_routes(model, te)
                m = summarize(y, pr)
                per_init.append({"metrics": {k: m[k] for k in
                                             ("ece", "pr_auc", "f1", "precision", "recall")},
                                 "routes": route_table(y, pr, rt),
                                 "n": int(y.size), "positives": int(y.sum())})

            mean_metrics = {k: float(np.mean([d["metrics"][k] for d in per_init]))
                            for k in per_init[0]["metrics"]}
            mean_routes = {}
            for _lo, _hi, lab in ROUTE_BINS:
                gaps = [d["routes"][lab]["gap"] for d in per_init if d["routes"][lab]["gap"] is not None]
                mean_routes[lab] = {
                    "n": int(np.mean([d["routes"][lab]["n"] for d in per_init])),
                    "positives": int(np.mean([d["routes"][lab]["positives"] for d in per_init])),
                    "mean_pred": float(np.mean([d["routes"][lab]["mean_pred"] for d in per_init
                                                if d["routes"][lab]["mean_pred"] is not None]))
                                 if gaps else None,
                    "observed": float(np.mean([d["routes"][lab]["observed"] for d in per_init
                                               if d["routes"][lab]["observed"] is not None]))
                                if gaps else None,
                    "gap": float(np.mean(gaps)) if gaps else None,
                }
            supported = all(
                mean_routes[b]["gap"] is not None and mean_routes[b]["n"] >= MIN_BIN_SUPPORT
                for b in ("1", "4+")
            )
            grad = (mean_routes["4+"]["gap"] - mean_routes["1"]["gap"]) if supported else None

            records[cond].append({
                "seed": part.seed, "metrics": mean_metrics, "routes": mean_routes,
                "gradient_4plus_minus_1": grad,
                "gradient_supported": bool(supported),
                "n_test": per_init[0]["n"], "positives": per_init[0]["positives"],
                "prevalence": per_init[0]["positives"] / max(per_init[0]["n"], 1),
                "test_quorum_nodes": quorum_nodes, "test_total_nodes": total_nodes,
            })
            done += 1
            gtxt = f"{grad:+.4f}" if grad is not None else "  n/a "
            print(f"  [{done}/{args.partitions}] seed {part.seed}: "
                  f"grad {gtxt}  ECE {mean_metrics['ece']:.4f}  "
                  f"PR {mean_metrics['pr_auc']:.4f}  F1 {mean_metrics['f1']:.4f}  "
                  f"quorum {quorum_nodes}/{total_nodes}  ({time.perf_counter()-t0:.0f}s)",
                  flush=True)

    runtime = time.perf_counter() - started
    after_p, after_f = hashes(PROTECTED), hashes(FROZEN)
    changed = [k for k in before_p if before_p[k] != after_p[k]]
    fchanged = [k for k in before_f if before_f[k] != after_f[k]]

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "quorum_control.json").write_text(json.dumps({
        "conditions": {c: {"quorum_fraction": s.quorum_fraction,
                           "resilient_fraction": s.resilient_fraction}
                       for c, s in CONDITIONS.items()},
        "frozen_settings": {"nodes_per_subgraph": list(CFG.nodes_per_subgraph),
                            "min_edges": CFG.min_edges, "epochs": EPOCHS,
                            "hidden": HIDDEN, "rounds": ROUNDS, "inits": args.inits,
                            "use_pos_weight": False, "threshold": 0.5},
        "gradient_definition": "gap(4+ routes) - gap(1 route)",
        "min_bin_support": MIN_BIN_SUPPORT,
        "runtime_seconds": runtime,
        "protected_unchanged": not changed, "frozen_unchanged": not fchanged,
        "records": records,
    }, indent=2), encoding="utf-8")
    print(f"\nRuntime {runtime/60:.1f} min. protected_unchanged={not changed} "
          f"frozen_unchanged={not fchanged}")


if __name__ == "__main__":
    main()
