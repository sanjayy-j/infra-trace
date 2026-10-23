"""
Controlled pos_weight ablation for SCP-GNN. DIAGNOSTIC, not a novelty claim.

QUESTION
--------
Does positive-class weighting cause SCP-GNN's raw probability overconfidence?
The calibration diagnostic measured raw ECE 0.1316 against logistic's 0.0378,
with a monotone overconfidence pattern. Weighted BCE with weight w has
minimiser p* = w*q / (w*q + (1-q)); at the observed w = 2.180 that predicts the
measured reliability curve with mean absolute error 0.043. That is a FIT to
existing data, not evidence. This run tests it.

ARMS (only B and C are trained here)
------------------------------------
  A  pos_weight = 2.180   baseline; metrics reused from calibration_diagnostic
  B  pos_weight = 1.0     use_pos_weight=False -> equal weights, native support
  C  pos_weight = 1.476   sqrt(2.180); partial reduction
  D  A + validation-fitted isotonic; reference only, already measured

HOW ARM C IS EXPRESSED WITHOUT EDITING PROTECTED CODE
-----------------------------------------------------
TrainConfig exposes only `use_pos_weight: bool`; there is no field for a fixed
numeric weight. `ml.train._pos_weight` has exactly ONE call site
(ml/train.py:208, inside train_model). Rather than modify ml/train.py - a
protected file whose hash is part of the validated checkpoint - this script
overrides that single function at runtime, asserts it returns the intended
value, and restores it in a finally block. No existing file is modified.

EVERYTHING ELSE IS FROZEN
-------------------------
Same 30 partitions and seeds, same pools, same sampling (40-120 nodes,
min_edges 25, 60/18/32), same 6 cascades, same 3 inits with
torch.manual_seed(part.seed*1000 + init), same architecture (32, 4), same 120
epochs, same early stopping on validation PR-AUC, same reachable-only mask,
same metric implementations, TEST-R primary. Arms are paired within partition
and initialisation.

LEAKAGE
-------
TEST-R labels are never used for training, early stopping, calibration fitting,
threshold selection or configuration selection. The fixed 0.5 threshold is
unchanged.

OUTPUTS (new files only)
------------------------
  results/posweight_ablation.json   metrics, reliability, configuration
  results/posweight_ablation.npz    retained predictions and labels
The frozen topology_v2.* and calibration_diagnostic.* artifacts are neither
read for writing nor modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import torch

from .. import train as train_mod
from ..evaluate import summarize
from ..graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM
from ..models import SCPConfig, SCPNet
from ..partition import SampleConfig, build_graph, sample_pool_subgraphs, stratified_split_node_pools
from ..realtopo import _truth_for
from ..synth import SynthConfig, simulate_cascade
from ..train import TrainConfig, train_model

RESULTS = Path(__file__).resolve().parent / "results"
ROOT = Path(__file__).resolve().parents[2]

# IDENTICAL to run_topology_v2.py / run_calibration_diagnostic.py
CFG = SampleConfig(nodes_per_subgraph=(40, 120), min_edges=25)
SCFG = SynthConfig()
N_TRAIN, N_VAL, N_TEST = 60, 18, 32
CASCADES, EPOCHS, HIDDEN, ROUNDS = 6, 120, 32, 4

BASELINE_W = 2.180
ARMS = {"B": {"use_pos_weight": False, "fixed_weight": None, "label": "unweighted (w=1.0)"},
        "C": {"use_pos_weight": True, "fixed_weight": float(np.sqrt(BASELINE_W)),
              "label": f"sqrt weight (w={float(np.sqrt(BASELINE_W)):.3f})"}}

SPLIT_CODE = {"val": 0, "test": 1}

PROTECTED = [
    "ml/models/scp.py", "ml/models/scp_bid.py", "ml/models/scp_latent.py",
    "ml/models/baselines.py", "ml/models/__init__.py", "ml/synth.py", "ml/graph.py",
    "ml/train.py", "ml/predict.py", "ml/train_production.py", "ml/realtopo.py",
    "ml/realistic.py", "ml/incomplete.py", "backend/app/ml_api.py",
    "backend/app/main.py", "ml/artifacts/scp_gnn.pt", "ml/artifacts/scp_linear.pt",
]
FROZEN_RESULTS = [
    "ml/experiments/results/topology_v2.json", "ml/experiments/results/topology_v2.log",
    "ml/experiments/results/calibration_diagnostic.json",
    "ml/experiments/results/calibration_diagnostic.log",
]


def hashes(paths) -> dict[str, str]:
    return {p: (hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                if (ROOT / p).exists() else "MISSING") for p in paths}


def build_dataset(pool, seed, count, rng):
    out = []
    for comps, deps, _keep in sample_pool_subgraphs(pool, seed, count, CFG):
        g = build_graph(comps, deps)
        if g.num_edges == 0:
            continue
        truth = _truth_for(g, deps, rng, SCFG)
        reach = np.array([g.reachable_from(i).sum() for i in range(g.num_nodes)], dtype=float)
        if reach.sum() == 0:
            continue
        w = reach / reach.sum()
        cas = [simulate_cascade(rng, g, truth, int(rng.choice(g.num_nodes, p=w)), SCFG)
               for _ in range(CASCADES)]
        out.append((g, cas))
    return out


@torch.no_grad()
def predictions_with_ids(model, dataset):
    """Per-decision predictions WITH identifiers, so retention is auditable.

    Mirrors ml.train.collect_predictions (same mask, same order) but also
    returns graph index, cascade index and component index for every row.
    """
    model.eval()
    y, p, gi, ci, ni = [], [], [], [], []
    for g_idx, (graph, cascades) in enumerate(dataset):
        t = train_mod.to_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        out = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots).cpu().numpy()
        for c_idx, cas in enumerate(cascades):
            m = cas.eval_mask(graph)          # reachable-only, unchanged
            if not m.any():
                continue
            idx = np.nonzero(m)[0]
            y.append(cas.labels[m]); p.append(out[c_idx][m])
            gi.append(np.full(idx.size, g_idx)); ci.append(np.full(idx.size, c_idx)); ni.append(idx)
    if not y:
        z = np.array([])
        return z, z, z, z, z
    return (np.concatenate(y), np.concatenate(p), np.concatenate(gi),
            np.concatenate(ci), np.concatenate(ni))


def reliability(y, p, bins=10):
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        sel = (p > lo) & (p <= hi) if i else (p >= lo) & (p <= hi)
        n = int(sel.sum())
        rows.append({"bin": f"({lo:.1f},{hi:.1f}]", "n": n,
                     "positives": int(y[sel].sum()) if n else 0,
                     "mean_confidence": float(p[sel].mean()) if n else None,
                     "observed_rate": float(y[sel].mean()) if n else None,
                     "gap": float(p[sel].mean() - y[sel].mean()) if n else None})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partitions", type=int, default=30)
    ap.add_argument("--inits", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="print config and exit")
    args = ap.parse_args()

    before_prot, before_froz = hashes(PROTECTED), hashes(FROZEN_RESULTS)

    accepted, rejected, p = [], [], 0
    while len(accepted) < 32 and p < 128:
        part = stratified_split_node_pools(p)
        (accepted if part.feasible else rejected).append(part if part.feasible else p)
        p += 1

    print("=" * 74)
    print("RESOLVED CONFIGURATION")
    print("=" * 74)
    print(f"  frozen sampling : nodes {CFG.nodes_per_subgraph}, min_edges {CFG.min_edges}, "
          f"{N_TRAIN}/{N_VAL}/{N_TEST}, cascades {CASCADES}")
    print(f"  frozen model    : hidden {HIDDEN}, rounds {ROUNDS}, epochs {EPOCHS}, "
          f"inits {args.inits}, threshold 0.5 (fixed)")
    print(f"  partitions      : {len(accepted)} accepted (seeds 0..{p-1}); rejected {rejected}")
    for name, cfg in ARMS.items():
        w = 1.0 if not cfg["use_pos_weight"] else cfg["fixed_weight"]
        print(f"  arm {name}: {cfg['label']:28} use_pos_weight={cfg['use_pos_weight']}  "
              f"effective w={w:.3f}")
    print(f"  arm A reference : w={BASELINE_W:.3f} (reused from calibration_diagnostic.json)")
    est = len(accepted[:args.partitions]) * args.inits * len(ARMS)
    print(f"  expected fits   : {est}")
    print(f"  outputs         : results/posweight_ablation.json, .npz")
    print(f"  protected files : {sum(1 for v in before_prot.values() if v!='MISSING')}/17 present")
    if args.dry_run:
        return

    records: dict[str, list] = {a: [] for a in ARMS}
    store = {k: [] for k in ("arm", "partition_seed", "init", "split", "graph_idx",
                             "cascade_idx", "node_idx", "label", "prob")}
    started = time.perf_counter()
    orig_pos_weight = train_mod._pos_weight

    try:
        for arm, acfg in ARMS.items():
            # --- express the arm's effective weight -------------------
            if acfg["fixed_weight"] is not None:
                fixed = float(acfg["fixed_weight"])
                train_mod._pos_weight = lambda *a, **k: fixed
                assert train_mod._pos_weight(None, True) == fixed
            else:
                train_mod._pos_weight = orig_pos_weight   # unused when flag is False
            print(f"\n=== ARM {arm}: {acfg['label']} ===", flush=True)

            done = 0
            for part in accepted:
                if done >= args.partitions:
                    break
                t0 = time.perf_counter()
                rng = np.random.default_rng(part.seed)
                tr = build_dataset(part.pools["train"], part.seed * 97 + 1, N_TRAIN, rng)
                va = build_dataset(part.pools["val"], part.seed * 97 + 2, N_VAL, rng)
                te = build_dataset(part.pools["test"], part.seed * 97 + 3, N_TEST, rng)
                if not (tr and va and te):
                    print(f"  seed {part.seed}: UNUSABLE", flush=True)
                    continue

                per_init = []
                rel0 = None
                for init in range(args.inits):
                    torch.manual_seed(part.seed * 1000 + init)
                    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, HIDDEN, ROUNDS))
                    train_model(model, tr, va,
                                TrainConfig(epochs=EPOCHS, seed=init,
                                            use_pos_weight=acfg["use_pos_weight"]))
                    for split, ds in (("val", va), ("test", te)):
                        y, pr, gi, ci, ni = predictions_with_ids(model, ds)
                        n = y.size
                        store["arm"].append(np.full(n, ord(arm), dtype=np.int8))
                        store["partition_seed"].append(np.full(n, part.seed, dtype=np.int16))
                        store["init"].append(np.full(n, init, dtype=np.int8))
                        store["split"].append(np.full(n, SPLIT_CODE[split], dtype=np.int8))
                        store["graph_idx"].append(gi.astype(np.int16))
                        store["cascade_idx"].append(ci.astype(np.int8))
                        store["node_idx"].append(ni.astype(np.int16))
                        store["label"].append(y.astype(np.int8))
                        store["prob"].append(pr.astype(np.float32))
                        if split == "test":
                            m = summarize(y, pr)
                            per_init.append({k: m[k] for k in
                                             ("ece", "pr_auc", "f1", "precision", "recall")})
                            if init == 0:
                                rel0 = reliability(y, pr)
                                n_test, n_pos = int(n), int(y.sum())

                mean = {k: float(np.mean([d[k] for d in per_init])) for k in per_init[0]}
                records[arm].append({"seed": part.seed, "mean": mean, "per_init": per_init,
                                     "n_test_decisions": n_test, "n_test_positives": n_pos,
                                     "reliability_init0": rel0})
                done += 1
                print(f"  [{done}/{args.partitions}] seed {part.seed}: "
                      f"ECE {mean['ece']:.4f} PR-AUC {mean['pr_auc']:.4f} F1 {mean['f1']:.4f} "
                      f"({time.perf_counter()-t0:.0f}s)", flush=True)
    finally:
        train_mod._pos_weight = orig_pos_weight

    runtime = time.perf_counter() - started
    after_prot, after_froz = hashes(PROTECTED), hashes(FROZEN_RESULTS)
    changed = [k for k in before_prot if before_prot[k] != after_prot[k]]
    froz_changed = [k for k in before_froz if before_froz[k] != after_froz[k]]

    RESULTS.mkdir(parents=True, exist_ok=True)
    arrays = {k: np.concatenate(v) for k, v in store.items() if v}
    lengths = {k: int(v.size) for k, v in arrays.items()}
    assert len(set(lengths.values())) == 1, f"array length mismatch: {lengths}"
    assert float(arrays["prob"].min()) >= 0.0 and float(arrays["prob"].max()) <= 1.0
    assert set(np.unique(arrays["label"]).tolist()) <= {0, 1}
    np.savez_compressed(RESULTS / "posweight_ablation.npz", **arrays)
    npz_mb = (RESULTS / "posweight_ablation.npz").stat().st_size / 1e6

    (RESULTS / "posweight_ablation.json").write_text(json.dumps({
        "frozen_settings": {"nodes_per_subgraph": list(CFG.nodes_per_subgraph),
                            "min_edges": CFG.min_edges,
                            "subgraphs": {"train": N_TRAIN, "val": N_VAL, "test": N_TEST},
                            "cascades": CASCADES, "epochs": EPOCHS, "hidden": HIDDEN,
                            "rounds": ROUNDS, "inits": args.inits, "threshold": 0.5},
        "arms": {a: {"label": c["label"],
                     "effective_weight": 1.0 if not c["use_pos_weight"] else c["fixed_weight"]}
                 for a, c in ARMS.items()},
        "baseline_arm_A_weight": BASELINE_W,
        "rejected_seeds": rejected,
        "runtime_seconds": runtime,
        "protected_unchanged": not changed,
        "frozen_results_unchanged": not froz_changed,
        "retained_rows": lengths, "npz_megabytes": npz_mb,
        "records": records,
    }, indent=2), encoding="utf-8")

    print(f"\nRuntime {runtime/60:.1f} min")
    print(f"Retained {lengths['prob']} prediction rows -> {npz_mb:.1f} MB")
    print(f"Protected unchanged: {not changed}   Frozen results unchanged: {not froz_changed}")


if __name__ == "__main__":
    main()
