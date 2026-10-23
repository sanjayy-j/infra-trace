"""
Calibration diagnostic for the corrected topology study.

PURPOSE
-------
The frozen acceptance rule failed on ECE regression (+0.0938, raw). The
specification record is ambiguous about whether the criterion meant RAW or
VALIDATION-CALIBRATED ECE: an earlier analysis-plan draft said "ECE after
validation-fitted isotonic calibration"; the final frozen criteria table said
only "ECE". Neither is on disk. This run measures both so the ambiguity can be
settled on evidence.

WHAT THIS IS NOT
----------------
It does not implement a structural-incompleteness floor, and it does not
support a novelty claim. It is a measurement.

A PRIOR FINDING THAT BOUNDS THIS DIAGNOSTIC
-------------------------------------------
Verified before writing this script: `eval_mask(reachable_only=True)` - the
default used by collect_predictions - excludes structurally unreachable
components from evaluation, and `simulate_cascade` never labels an unreachable
component positive (propagation follows recorded edges only; false-positive
noise is confined to reachable nodes). Measured on the frozen configuration:
0 of 902 evaluated decisions were unreachable, and 0 of 248 positives.

Therefore the reachable/unreachable subset split requested for this diagnostic
is VACUOUS - the unreachable subset is empty - and the missing-edge hypothesis
cannot be tested with these labels. The split is still computed and reported so
the emptiness is visible in the output rather than asserted here.

LEAKAGE
-------
The isotonic map is fitted on VALIDATION predictions and labels only, then
applied to held-out TEST-R predictions. Test labels never touch the fit. The
script asserts this by fitting before test predictions are produced, and by
recording the sample counts of each split.

Writes to results/calibration_diagnostic.json. The original topology outputs
(topology_v2.json, topology_v2.log) are not read, modified or overwritten.

Run:
    python -m ml.experiments.run_calibration_diagnostic --partitions 30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import torch

from ..evaluate import expected_calibration_error, fit_isotonic, summarize
from ..graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM
from ..models import LogisticBaseline, SCPConfig, SCPNet
from ..partition import SampleConfig, build_graph, sample_pool_subgraphs, stratified_split_node_pools
from ..realtopo import _truth_for
from ..synth import SynthConfig, simulate_cascade
from ..train import TrainConfig, collect_predictions, collect_predictions_classical, train_model

RESULTS = Path(__file__).resolve().parent / "results"
ROOT = Path(__file__).resolve().parents[2]

# IDENTICAL to run_topology_v2.py. Any divergence invalidates the comparison.
CFG = SampleConfig(nodes_per_subgraph=(40, 120), min_edges=25)
SCFG = SynthConfig()
N_TRAIN, N_VAL, N_TEST = 60, 18, 32
CASCADES, EPOCHS, HIDDEN, ROUNDS = 6, 120, 32, 4

PROTECTED = [
    "ml/models/scp.py", "ml/models/scp_bid.py", "ml/models/scp_latent.py",
    "ml/models/baselines.py", "ml/models/__init__.py", "ml/synth.py", "ml/graph.py",
    "ml/train.py", "ml/predict.py", "ml/train_production.py", "ml/realtopo.py",
    "ml/realistic.py", "ml/incomplete.py", "backend/app/ml_api.py",
    "backend/app/main.py", "ml/artifacts/scp_gnn.pt", "ml/artifacts/scp_linear.pt",
]


def hash_protected() -> dict[str, str]:
    return {r: (hashlib.sha256((ROOT / r).read_bytes()).hexdigest()
                if (ROOT / r).exists() else "MISSING") for r in PROTECTED}


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


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """Per-bin reliability summary: counts, mean confidence, observed rate."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        sel = (p > lo) & (p <= hi) if i else (p >= lo) & (p <= hi)
        n = int(sel.sum())
        rows.append({
            "bin": f"({lo:.1f},{hi:.1f}]",
            "n": n,
            "positives": int(y[sel].sum()) if n else 0,
            "mean_confidence": float(p[sel].mean()) if n else None,
            "observed_rate": float(y[sel].mean()) if n else None,
            "gap": float(p[sel].mean() - y[sel].mean()) if n else None,
        })
    return rows


def reachability_split(dataset):
    """Flags for each evaluated decision: is it reachable from its root?

    Expected to be all-True; computed so the emptiness of the unreachable
    subset is demonstrated by the output rather than asserted.
    """
    flags = []
    for graph, cascades in dataset:
        for cas in cascades:
            m = cas.eval_mask(graph)
            if not m.any():
                continue
            flags.append(graph.reachable_from(cas.root_index)[m])
    return np.concatenate(flags) if flags else np.array([], dtype=bool)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partitions", type=int, default=30)
    ap.add_argument("--inits", type=int, default=3)
    ap.add_argument("--max-scan", type=int, default=128)
    args = ap.parse_args()

    before = hash_protected()
    started = time.perf_counter()

    accepted, rejected, p = [], [], 0
    while len(accepted) < 32 and p < args.max_scan:
        part = stratified_split_node_pools(p)
        (accepted if part.feasible else rejected).append(part if part.feasible else p)
        p += 1
    print(f"Partitions: {len(accepted)} accepted (seeds 0..{p-1}), rejected {rejected}")

    records, skipped = [], []
    for part in accepted:
        if len(records) >= args.partitions:
            break
        t0 = time.perf_counter()
        rng = np.random.default_rng(part.seed)
        train_ds = build_dataset(part.pools["train"], part.seed * 97 + 1, N_TRAIN, rng)
        val_ds = build_dataset(part.pools["val"], part.seed * 97 + 2, N_VAL, rng)
        test_ds = build_dataset(part.pools["test"], part.seed * 97 + 3, N_TEST, rng)
        if not (train_ds and val_ds and test_ds):
            skipped.append(part.seed)
            print(f"  seed {part.seed}: UNUSABLE (val/test empty)", flush=True)
            continue

        reach_flags = reachability_split(test_ds)

        raw_ece, cal_ece, raw_pr, cal_pr, raw_f1, cal_f1 = [], [], [], [], [], []
        rel_raw, rel_cal = None, None
        n_val_used = n_test_used = 0

        for init in range(args.inits):
            torch.manual_seed(part.seed * 1000 + init)
            model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, HIDDEN, ROUNDS))
            train_model(model, train_ds, val_ds, TrainConfig(epochs=EPOCHS, seed=init))

            # Fit the calibrator on VALIDATION ONLY, before touching test.
            y_val, p_val = collect_predictions(model, val_ds)
            iso = fit_isotonic(y_val, p_val)
            n_val_used = int(y_val.size)

            y_te, p_te = collect_predictions(model, test_ds)
            n_test_used = int(y_te.size)
            r = summarize(y_te, p_te)
            raw_ece.append(r["ece"]); raw_pr.append(r["pr_auc"]); raw_f1.append(r["f1"])
            if init == 0:
                rel_raw = reliability(y_te, p_te)

            if iso is not None:
                p_cal = np.asarray(iso.predict(p_te))
                c = summarize(y_te, p_cal)
                cal_ece.append(c["ece"]); cal_pr.append(c["pr_auc"]); cal_f1.append(c["f1"])
                if init == 0:
                    rel_cal = reliability(y_te, p_cal)

        lg = LogisticBaseline().fit(train_ds)
        y_l, p_l = collect_predictions_classical(lg, test_ds)
        lg_m = summarize(y_l, p_l)

        records.append({
            "seed": part.seed,
            "n_val_decisions": n_val_used,
            "n_test_decisions": n_test_used,
            "n_test_positives": int(y_te.sum()),
            "test_reachable": int(reach_flags.sum()),
            "test_unreachable": int((~reach_flags).sum()),
            "scp_raw": {"ece": float(np.mean(raw_ece)), "pr_auc": float(np.mean(raw_pr)),
                        "f1": float(np.mean(raw_f1))},
            "scp_calibrated": ({"ece": float(np.mean(cal_ece)), "pr_auc": float(np.mean(cal_pr)),
                                "f1": float(np.mean(cal_f1))} if cal_ece else None),
            "logistic": {"ece": lg_m["ece"], "pr_auc": lg_m["pr_auc"], "f1": lg_m["f1"]},
            "reliability_raw_init0": rel_raw,
            "reliability_calibrated_init0": rel_cal,
        })
        print(f"  [{len(records)}/{args.partitions}] seed {part.seed}: ok "
              f"({time.perf_counter()-t0:.0f}s)", flush=True)

    runtime = time.perf_counter() - started
    after = hash_protected()
    changed = [k for k in before if before[k] != after[k]]

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "calibration_diagnostic.json"
    out.write_text(json.dumps({
        "frozen_settings_match_topology_v2": {
            "nodes_per_subgraph": list(CFG.nodes_per_subgraph), "min_edges": CFG.min_edges,
            "subgraphs": {"train": N_TRAIN, "val": N_VAL, "test": N_TEST},
            "cascades": CASCADES, "epochs": EPOCHS, "hidden": HIDDEN, "rounds": ROUNDS,
            "inits": args.inits,
        },
        "rejected_seeds": rejected,
        "skipped_unusable_seeds": skipped,
        "runtime_seconds": runtime,
        "protected_unchanged": not changed,
        "partitions": records,
    }, indent=2), encoding="utf-8")
    print(f"\nRuntime {runtime/60:.1f} min. Wrote {out}")
    print(f"Protected unchanged: {not changed}")


if __name__ == "__main__":
    main()
