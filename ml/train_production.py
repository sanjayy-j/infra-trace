"""
Train the model the API serves, and save it as a versioned artifact.

Training is a separate operation from inference. This script writes
ml/artifacts/scp_gnn.pt; the API only ever reads it. Nothing in the request
path trains, fits, or mutates a model.

WHAT GOES IN THE ARTIFACT
-------------------------
    state_dict    the learned weights
    config        architecture and feature dimensions, so load_predictor can
                  refuse a model whose feature encoding no longer matches
    calibrator    isotonic map fitted on the validation split
    provenance    seed, data settings, package version, UTC timestamp and
                  the validation metrics achieved

Provenance is stored because a prediction with no record of which model
produced it, from which data, is not reproducible, and an artifact that
cannot be traced back to a training run should not be making operational
recommendations.

A NOTE ON THE CALIBRATOR
------------------------
It is fitted on synthetic validation data, whose positive rate (~26%) does
not match the real ShopSphere incident data (~44%). Calibration therefore
transfers imperfectly, and the measured effect is reported in
docs/ml/experiments.md rather than assumed away. An organisation adopting
this should refit the calibrator on its own incident history, which is the
one step that genuinely needs local data; the transmission function itself
transfers without it.

Run:
    python -m ml.train_production
    python -m ml.train_production --epochs 200 --seed 3
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

from .evaluate import fit_isotonic, summarize
from .graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM
from .models import SCPConfig, SCPNet
from .synth import generate_dataset
from .train import TrainConfig, collect_predictions, train_model

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--epochs", type=int, default=150)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--hidden", type=int, default=32)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument(
        "--linear-link",
        action="store_true",
        help=(
            "Use the 28-parameter linear transmission function instead of the "
            "MLP. Measured as equivalent on every metric at the best threshold "
            "(docs/ml/parsimony-study.md) and readable with `python -m "
            "ml.explain`. Not the default: the MLP artifact is the published "
            "baseline and switching it silently would invalidate the recorded "
            "results."
        ),
    )
    ap.add_argument("--train-graphs", type=int, default=90)
    ap.add_argument("--val-graphs", type=int, default=25)
    ap.add_argument("--out", type=Path, default=ARTIFACT_DIR / "scp_gnn.pt")
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    print(f"Generating training data (seed {args.seed}) ...")
    train_ds = generate_dataset(args.seed * 100 + 1, args.train_graphs, 6)
    val_ds = generate_dataset(args.seed * 100 + 2, args.val_graphs, 6)
    print(f"  {len(train_ds)} training graphs, {len(val_ds)} validation graphs")

    model = SCPNet(
        SCPConfig(
            NODE_FEATURE_DIM, EDGE_FEATURE_DIM, args.hidden, args.rounds,
            linear_link=args.linear_link,
        )
    )
    print(f"Training SCP-GNN ({sum(p.numel() for p in model.parameters())} parameters) ...")
    history = train_model(
        model, train_ds, val_ds, TrainConfig(epochs=args.epochs, seed=args.seed, verbose=True)
    )
    print(
        f"  best validation PR-AUC {history['best_val_pr_auc']:.4f} "
        f"at epoch {history['best_epoch']} "
        f"({history['train_seconds']:.1f}s)"
    )

    y_val, p_val = collect_predictions(model, val_ds)
    before = summarize(y_val, p_val)
    calibrator = fit_isotonic(y_val, p_val)
    after = (
        summarize(y_val, np.asarray(calibrator.predict(p_val)))
        if calibrator is not None
        else before
    )
    # IN-SAMPLE: the calibrator was fitted on exactly this split, so this
    # number is near zero by construction and says nothing about how well
    # calibration generalises. The honest out-of-sample figure is
    # `ece_calibrated` in the benchmark, which fits on validation and scores
    # on unseen test topologies (~0.05). Printed only to confirm the
    # calibrator fitted at all.
    print(
        f"  validation ECE {before['ece']:.4f} -> {after['ece']:.4f} "
        "(IN-SAMPLE, fitted on this split - see docs/ml/experiments.md)"
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "config": {
                "node_dim": NODE_FEATURE_DIM,
                "edge_dim": EDGE_FEATURE_DIM,
                "hidden": args.hidden,
                "rounds": args.rounds,
                "linear_link": args.linear_link,
            },
            "calibrator": calibrator,
            "provenance": {
                "trained_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "seed": args.seed,
                "epochs": args.epochs,
                "train_graphs": len(train_ds),
                "val_graphs": len(val_ds),
                "data": "synthetic (ml/synth.py); NOT trained on real incidents",
                "torch": torch.__version__,
                "val_metrics_raw": {k: v for k, v in before.items()},
                # In-sample for the calibrator; see the note where this is printed.
                "val_metrics_calibrated_in_sample": {k: v for k, v in after.items()},
                "history": history,
            },
        },
        args.out,
    )
    print(f"\nWrote {args.out}")
    print(
        json.dumps(
            {
                "val_pr_auc": before["pr_auc"],
                "val_ece_raw": before["ece"],
                "val_ece_calibrated_in_sample": after["ece"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
