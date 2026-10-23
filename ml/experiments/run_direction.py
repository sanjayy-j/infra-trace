"""
Directionality experiment: does modelling forward propagation help?

THE QUESTION
------------
7 of 37 recorded impacts are reachable only by following a dependency edge
FORWARDS. Admitting forward edges lifts the recall ceiling from 78.4% to
97.3% but inflates the candidate set 3.4x and drops reachability precision
from 43.9% to 16.1%. Can a learned, direction-specific transmission function
keep the recall and recover the precision?

PROTOCOL
--------
Every model is scored on the SAME candidate set - components reachable in
EITHER direction. Reverse-only models predict exactly 0 on forward-only
candidates, which is the real cost of ignoring forward propagation and should
show up as lost recall. Scoring each model on its own reachable set would
compare them on different questions and would flatter the reverse-only ones.

Training data has forward propagation ENABLED (it is off by default, so the
original benchmark stays reproducible). The forward mechanism in ml/synth.py
is retry amplification, which scales with how many of the failing component's
callers have already failed - a function of live cascade state that noisy-OR
cannot represent. Without that, generating forward propagation as independent
per-edge draws would hand SCP-BiD data from its own hypothesis class.

THE ABLATION THAT CARRIES THE ARGUMENT
--------------------------------------
"SCP-BiD, direction-blind" sees exactly the same bidirectional edges but has
the `is_forward` flag zeroed. Comparing the proposal only against reverse-only
SCP-GNN would conflate "more edges help" with "direction matters"; this
separates them.

Run:
    python -m ml.experiments.run_direction --seeds 10
    python -m ml.experiments.run_direction --seeds 3 --quick
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
from scipy import stats

from ..evaluate import aggregate, fit_isotonic, format_table, summarize
from ..graph import BI_EDGE_FEATURE_DIM, EDGE_FEATURE_DIM, NODE_FEATURE_DIM
from ..models import (
    DepthDecayBaseline,
    GCNNet,
    LogisticBaseline,
    ReachabilityBaseline,
    SCPConfig,
    SCPNet,
)
from ..models.baselines import BidirectionalReachabilityBaseline
from ..models.scp_bid import SCPBiDConfig, SCPBiDNet
from ..synth import SynthConfig, generate_dataset
from ..train import (
    TrainConfig,
    collect_predictions,
    collect_predictions_classical,
    train_model,
)

RESULTS = Path(__file__).resolve().parent / "results"

# Forward propagation ON: this experiment is about a mechanism that the
# default generator deliberately omits.
FORWARD_CFG = SynthConfig(enable_forward_propagation=True)


def build_splits(seed: int, quick: bool):
    n_train, n_val, n_test = (24, 8, 16) if quick else (70, 20, 40)
    return (
        generate_dataset(seed * 1000 + 1, n_train, 6, FORWARD_CFG),
        generate_dataset(seed * 1000 + 2, n_val, 6, FORWARD_CFG),
        generate_dataset(seed * 1000 + 3, n_test, 6, FORWARD_CFG),
    )


def _neural(name, ctor, train_ds, val_ds, test_ds, seed, epochs, bi_edges):
    """Train one torch model and score it on the bidirectional candidate set."""
    torch.manual_seed(seed)
    model = ctor()
    cfg = TrainConfig(
        epochs=epochs,
        seed=seed,
        bidirectional_mask=True,       # same candidate set for everyone
        bidirectional_edges=bi_edges,  # but only some models see forward edges
    )
    hist = train_model(model, train_ds, val_ds, cfg)

    started = time.perf_counter()
    y, p = collect_predictions(
        model, test_ds, True, "cpu", bidirectional_mask=True, bidirectional_edges=bi_edges
    )
    infer_s = time.perf_counter() - started

    res = summarize(y, p)
    res["params"] = sum(q.numel() for q in model.parameters())
    res["train_seconds"] = hist["train_seconds"]
    res["inference_ms_per_scenario"] = 1000.0 * infer_s / max(
        sum(len(c) for _, c in test_ds), 1
    )

    y_val, p_val = collect_predictions(
        model, val_ds, True, "cpu", bidirectional_mask=True, bidirectional_edges=bi_edges
    )
    iso = fit_isotonic(y_val, p_val)
    if iso is not None:
        cal = summarize(y, np.asarray(iso.predict(p)))
        res["ece_calibrated"] = cal["ece"]
    return res


def run_seed(seed: int, quick: bool, epochs: int) -> dict[str, dict]:
    train_ds, val_ds, test_ds = build_splits(seed, quick)
    out: dict[str, dict] = {}

    # ---- structural baselines ----
    for name, model in (
        ("Reachability reverse (SQL today)", ReachabilityBaseline()),
        ("Reachability bidirectional", BidirectionalReachabilityBaseline()),
    ):
        y, p = collect_predictions_classical(model, test_ds, True, bidirectional_mask=True)
        out[name] = summarize(y, p)

    decay = DepthDecayBaseline().fit(train_ds)
    y, p = collect_predictions_classical(decay, test_ds, True, bidirectional_mask=True)
    out["Depth decay (reverse)"] = summarize(y, p)

    for name, bi in (("Logistic reverse feats", False), ("Logistic bidirectional feats", True)):
        lg = LogisticBaseline(bidirectional=bi).fit(train_ds)
        y, p = collect_predictions_classical(lg, test_ds, True, bidirectional_mask=True)
        out[name] = summarize(y, p)

    # ---- neural ----
    out["GCN (reverse edges)"] = _neural(
        "gcn-rev", lambda: GCNNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4),
        train_ds, val_ds, test_ds, seed, epochs, bi_edges=False)

    out["GCN (bidirectional edges)"] = _neural(
        "gcn-bi", lambda: GCNNet(NODE_FEATURE_DIM, BI_EDGE_FEATURE_DIM, 32, 4),
        train_ds, val_ds, test_ds, seed, epochs, bi_edges=True)

    out["SCP-GNN (reverse only)"] = _neural(
        "scp", lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        train_ds, val_ds, test_ds, seed, epochs, bi_edges=False)

    out["SCP-BiD (proposed)"] = _neural(
        "scpbid", lambda: SCPBiDNet(SCPBiDConfig(NODE_FEATURE_DIM, hidden=32, rounds=4)),
        train_ds, val_ds, test_ds, seed, epochs, bi_edges=True)

    out["  ablation: BiD direction-blind"] = _neural(
        "scpbid-blind",
        lambda: SCPBiDNet(
            SCPBiDConfig(NODE_FEATURE_DIM, hidden=32, rounds=4, use_direction_flag=False)
        ),
        train_ds, val_ds, test_ds, seed, epochs, bi_edges=True)

    return out


def paired_report(per_seed: dict[str, list[dict]], base: str, metrics=("pr_auc", "f1")) -> None:
    print(f"\nPaired comparisons vs {base}")
    print(f"{'vs':34} {'metric':8} {'mean diff':>10} {'95% CI':>22} {'t p':>9} {'wins':>7}")
    print("-" * 96)
    for other in per_seed:
        if other == base:
            continue
        for m in metrics:
            a = np.array([r[m] for r in per_seed[base]], dtype=float)
            b = np.array([r[m] for r in per_seed[other]], dtype=float)
            ok = np.isfinite(a) & np.isfinite(b)
            a, b = a[ok], b[ok]
            if a.size < 2 or np.allclose(a, b):
                continue
            d = a - b
            se = d.std(ddof=1) / np.sqrt(d.size)
            ci = stats.t.interval(0.95, d.size - 1, loc=d.mean(), scale=se)
            pv = stats.ttest_rel(a, b).pvalue
            mark = "***" if pv < 0.001 else "**" if pv < 0.01 else "*" if pv < 0.05 else " ns"
            print(f"{other.strip():34} {m:8} {d.mean():+10.4f} "
                  f"[{ci[0]:+.4f},{ci[1]:+.4f}] {pv:9.4f}{mark} {int((d>0).sum()):3d}/{d.size}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    per_seed: dict[str, list[dict]] = {}
    for seed in range(args.seeds):
        print(f"--- seed {seed} ---", flush=True)
        for name, res in run_seed(seed, args.quick, args.epochs).items():
            per_seed.setdefault(name, []).append(res)

    summary = {k: aggregate(v) for k, v in per_seed.items()}
    rows = [(k, summary[k]) for k in per_seed]

    print()
    print(f"Directionality, {args.seeds} seeds, unseen topologies, forward propagation ON")
    print("All models scored on the BIDIRECTIONAL candidate set (mean +- std)")
    print()
    print(format_table(rows, ("pr_auc", "precision", "recall", "f1", "ece_calibrated")))

    paired_report(per_seed, "SCP-BiD (proposed)")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "direction.json"
    path.write_text(
        json.dumps({"seeds": args.seeds, "summary": summary, "per_seed": per_seed}, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
