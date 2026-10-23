"""
Benchmark: SCP-GNN against baselines and its own ablations.

PROTOCOL
--------
Split is BY GRAPH, not by cascade. Every test graph is a topology the model
has never seen - a different number of components, different wiring,
different edge-type mix. Splitting by cascade would leak: two incidents on
the same graph share all their structure, so a model could memorise that
graph's layout during training and be scored on it at test time. Splitting
by graph makes this an inductive generalisation test, which is the setting
InfraTrace actually faces when a team adds a service.

Every model sees identical splits and identical features. Results are
averaged over several seeds, each of which re-draws the data, re-initialises
the weights, and re-runs training; the standard deviation across seeds is
reported because a single run on generated data is not evidence of anything.

Run:
    python -m ml.experiments.run_benchmark --seeds 5
    python -m ml.experiments.run_benchmark --seeds 3 --quick
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch

from ..evaluate import aggregate, fit_isotonic, format_table, summarize
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
from ..synth import SynthConfig, generate_dataset
from ..train import (
    TrainConfig,
    collect_predictions,
    collect_predictions_classical,
    train_model,
)

ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"
RESULTS = Path(__file__).resolve().parent / "results"


def build_splits(seed: int, quick: bool, cfg: SynthConfig | None = None):
    """Generate disjoint train / validation / test graph sets."""
    n_train, n_val, n_test = (24, 8, 16) if quick else (70, 20, 40)
    per_graph = 6
    train = generate_dataset(seed * 1000 + 1, n_train, per_graph, cfg)
    val = generate_dataset(seed * 1000 + 2, n_val, per_graph, cfg)
    test = generate_dataset(seed * 1000 + 3, n_test, per_graph, cfg)
    return train, val, test


def _torch_model_specs(hidden: int, rounds: int):
    """Name -> (constructor, forward kwargs, train kwargs)."""
    return {
        "GCN": (lambda: GCNNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden, rounds), {}, {}),
        "GraphSAGE": (lambda: SAGENet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden, rounds), {}, {}),
        "GAT": (lambda: GATNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden, rounds), {}, {}),
        "SCP-GNN (proposed)": (
            lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden, rounds)),
            {},
            {},
        ),
        # --- ablations ---
        "  ablation: mean-agg": (
            lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden, rounds)),
            {"aggregator": "mean"},
            {},
        ),
        "  ablation: no-edge-feats": (
            lambda: SCPNet(
                SCPConfig(
                    NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden, rounds,
                    use_edge_features=False,
                )
            ),
            {},
            {},
        ),
    }


def run_seed(seed: int, quick: bool, epochs: int) -> dict[str, dict]:
    """Train and evaluate every model once. Returns name -> metrics."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    train_ds, val_ds, test_ds = build_splits(seed, quick)
    results: dict[str, dict] = {}

    # ---- non-neural baselines ----
    reach = ReachabilityBaseline()
    y, p = collect_predictions_classical(reach, test_ds)
    results["Reachability (current SQL)"] = summarize(y, p)

    decay = DepthDecayBaseline().fit(train_ds)
    y, p = collect_predictions_classical(decay, test_ds)
    results["Depth decay"] = summarize(y, p)
    results["Depth decay"]["fitted_lambda"] = decay.lam

    logistic = LogisticBaseline().fit(train_ds)
    y, p = collect_predictions_classical(logistic, test_ds)
    results["Logistic (path feats)"] = summarize(y, p)

    # ---- neural models ----
    for name, (ctor, fwd_kwargs, _) in _torch_model_specs(32, 4).items():
        model = ctor()
        tcfg = TrainConfig(epochs=epochs, seed=seed)
        hist = train_model(model, train_ds, val_ds, tcfg, **fwd_kwargs)

        started = time.perf_counter()
        y, p = collect_predictions(model, test_ds, **fwd_kwargs)
        infer_s = time.perf_counter() - started

        res = summarize(y, p)
        res["train_seconds"] = hist["train_seconds"]
        res["inference_ms_per_scenario"] = 1000.0 * infer_s / max(
            sum(len(c) for _, c in test_ds), 1
        )
        res["params"] = sum(q.numel() for q in model.parameters())

        # Recalibrate on validation, score on test.
        #
        # Isotonic regression is non-decreasing, so it never INVERTS the
        # order of two predictions. It does, however, map distinct scores
        # onto the same value, and average precision scores ties differently
        # from strict orderings - so PR-AUC can shift slightly even though
        # nothing was reordered. The assertion below therefore checks the
        # property that actually holds (monotonicity of the fitted map) and
        # the calibrated PR-AUC is reported alongside the raw one rather
        # than assumed equal to it.
        y_val, p_val = collect_predictions(model, val_ds, **fwd_kwargs)
        iso = fit_isotonic(y_val, p_val)
        if iso is not None:
            probe = np.linspace(0.0, 1.0, 101)
            mapped = np.asarray(iso.predict(probe))
            # Tolerance is float32 epsilon, not zero: the calibrator returns
            # float32, so two genuinely equal plateau values can differ by
            # ~6e-8 and a stricter bound flags rounding noise as a defect.
            assert np.all(np.diff(mapped) >= -1e-6), "calibrator is not monotone"
            cal = summarize(y, np.asarray(iso.predict(p)))
            res["pr_auc_calibrated"] = cal["pr_auc"]
            res["ece_calibrated"] = cal["ece"]
            res["brier_calibrated"] = cal["brier"]
        results[name] = res

        if name == "SCP-GNN (proposed)" and seed == 0:
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            torch.save(
                {
                    "state_dict": model.state_dict(),
                    "config": {
                        "node_dim": NODE_FEATURE_DIM,
                        "edge_dim": EDGE_FEATURE_DIM,
                        "hidden": 32,
                        "rounds": 4,
                    },
                    "trained_on": "synthetic",
                    "seed": seed,
                },
                ARTIFACTS / "scp_gnn.pt",
            )

    # ---- ablation that changes the TRAINING procedure, not the model ----
    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4))
    train_model(model, train_ds, val_ds, TrainConfig(epochs=epochs, seed=seed, reach_mask=False))
    y, p = collect_predictions(model, test_ds, reach_mask=True)
    results["  ablation: no-reach-mask"] = summarize(y, p)

    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--quick", action="store_true", help="smaller splits, for a fast check")
    args = ap.parse_args()

    per_seed: dict[str, list[dict]] = {}
    for seed in range(args.seeds):
        print(f"--- seed {seed} ---", flush=True)
        for name, res in run_seed(seed, args.quick, args.epochs).items():
            per_seed.setdefault(name, []).append(res)

    summary = {name: aggregate(runs) for name, runs in per_seed.items()}

    metrics = ("pr_auc", "precision", "recall", "f1", "ece", "ece_calibrated")
    order = [
        "Reachability (current SQL)",
        "Depth decay",
        "Logistic (path feats)",
        "GCN",
        "GraphSAGE",
        "GAT",
        "SCP-GNN (proposed)",
        "  ablation: mean-agg",
        "  ablation: no-edge-feats",
        "  ablation: no-reach-mask",
    ]
    rows = [(n, summary[n]) for n in order if n in summary]

    print()
    print(f"Blast-radius prediction, {args.seeds} seeds, unseen test topologies")
    print("Metrics on components reachable from the root (mean +- std across seeds)")
    print()
    print(format_table(rows, metrics))
    print()
    print("Reachability is the system as it stands: it predicts 1.0 for every")
    print("reachable component, so its recall is 1.0 and its precision is the")
    print("positive rate. PR-AUC for it is the no-skill line.")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "benchmark.json"
    out.write_text(
        json.dumps(
            {"seeds": args.seeds, "quick": args.quick, "summary": summary,
             "per_seed": per_seed},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
