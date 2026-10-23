"""
Read the transmission function's coefficients.

WHY THIS EXISTS
---------------
docs/ml/novelty.md established that the 28-parameter LINEAR transmission
function matches the 1,441-parameter MLP. A 15-seed confirmation
(docs/ml/parsimony-study.md) found no difference at the best threshold on
either synthetic or real data.

That equivalence is worth something the accuracy numbers are not: with a
linear link the model IS 28 numbers, and they can be printed, checked against
reality, and argued with. `sigma(theta^T x)` means each feature's
contribution to a dependency's transmission probability is a single signed
weight. Nothing here is a novel method; it is the practical payoff of
preferring the smaller model when the larger one buys nothing.

WHAT TO LOOK FOR
----------------
Sign stability across seeds is the thing to check. A weight whose sign flips
between runs is noise regardless of magnitude, and reading meaning into it
would be over-claiming. The edge-feature weights are stable (100% sign
agreement over 10 seeds); several node-feature weights are not, which is
itself informative - they contribute little the model relies on.

This module only reads a model. It never trains, modifies or replaces one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch

from .graph import (
    EDGE_FEATURE_NAMES,
    NODE_FEATURE_NAMES,
)
from .models import SCPConfig, SCPNet


def feature_names() -> list[str]:
    """Names for the transmission function's input, in column order.

    Must match the concatenation in EdgeTransmissionNet.forward:
    [ x_src ; x_dst ; z_edge ].
    """
    return (
        [f"src:{n}" for n in NODE_FEATURE_NAMES]
        + [f"dst:{n}" for n in NODE_FEATURE_NAMES]
        + list(EDGE_FEATURE_NAMES)
    )


@dataclass
class Coefficient:
    name: str
    weight: float
    std: float
    sign_stability: float  # share of seeds agreeing with the mean's sign

    @property
    def trustworthy(self) -> bool:
        """Whether the sign is consistent enough to interpret at all."""
        return self.sign_stability >= 0.9


def linear_coefficients(models: list[SCPNet]) -> list[Coefficient]:
    """Extract and summarise coefficients across several trained linear models.

    Raises if given a model whose link is not linear - an MLP has no single
    weight per feature, and silently reporting its first layer would be
    meaningless.
    """
    if not models:
        raise ValueError("no models supplied")

    weights = []
    for m in models:
        net = m.edge_net.net
        if not isinstance(net, torch.nn.Linear):
            raise ValueError(
                "coefficients are only defined for a linear transmission "
                "function. Build the model with SCPConfig(linear_link=True); "
                "an MLP has no one weight per feature."
            )
        weights.append(net.weight.detach().cpu().numpy().ravel())

    w = np.asarray(weights)
    mean = w.mean(axis=0)
    std = w.std(axis=0, ddof=1) if w.shape[0] > 1 else np.zeros_like(mean)
    stability = (np.sign(w) == np.sign(mean)).mean(axis=0)

    names = feature_names()
    if len(names) != mean.shape[0]:
        raise ValueError(
            f"feature-name list has {len(names)} entries but the model has "
            f"{mean.shape[0]} weights; ml/graph.py and ml/explain.py have "
            "drifted apart."
        )

    return [
        Coefficient(names[i], float(mean[i]), float(std[i]), float(stability[i]))
        for i in range(mean.shape[0])
    ]


def format_coefficients(coefs: list[Coefficient], top: int | None = None) -> str:
    """Render coefficients sorted by magnitude, flagging unstable signs."""
    ordered = sorted(coefs, key=lambda c: -abs(c.weight))
    if top is not None:
        ordered = ordered[:top]

    lines = [
        f"{'feature':34}{'weight':>18}{'sign stable':>13}",
        "-" * 65,
    ]
    for c in ordered:
        flag = "" if c.trustworthy else "   <- unstable, do not interpret"
        lines.append(
            f"{c.name:34}{c.weight:>+10.3f}+-{c.std:.3f}"
            f"{c.sign_stability * 100:>11.0f}%{flag}"
        )
    return "\n".join(lines)


def edge_type_ranking(coefs: list[Coefficient]) -> list[tuple[str, float]]:
    """Transmission weight per dependency type, with and without `is_critical`.

    This is the output that can be checked against reality: the ordering
    should reproduce the rates measured on the real incidents in
    docs/ml/design.md (Synchronous+critical 85.7%, Synchronous 50%, Data 50%,
    Asynchronous 40%). The model never sees those rates during training, so
    agreement is independent corroboration rather than a fit.
    """
    by_name = {c.name: c.weight for c in coefs}
    critical = by_name.get("is_critical", 0.0)
    out: list[tuple[str, float]] = []
    for name, weight in by_name.items():
        if not name.startswith("dep_type="):
            continue
        label = name.split("=", 1)[1]
        out.append((label, weight))
        out.append((f"{label}+critical", weight + critical))
    return sorted(out, key=lambda kv: -kv[1])


def main() -> None:
    """Train a few linear models and print what they learned."""
    import argparse

    from .synth import generate_dataset
    from .train import TrainConfig, train_model
    from .graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--top", type=int, default=14)
    args = ap.parse_args()

    models = []
    for seed in range(args.seeds):
        train_ds = generate_dataset(seed * 100 + 11, 70, 6)
        val_ds = generate_dataset(seed * 100 + 12, 20, 6)
        torch.manual_seed(seed)
        model = SCPNet(
            SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4, linear_link=True)
        )
        train_model(model, train_ds, val_ds, TrainConfig(epochs=args.epochs, seed=seed))
        models.append(model)

    coefs = linear_coefficients(models)
    print(f"Linear transmission function, {args.seeds} seeds\n")
    print(format_coefficients(coefs, top=args.top))
    print()
    print("Dependency type ranking (higher = transmits failure more readily):")
    print("-" * 65)
    for label, weight in edge_type_ranking(coefs):
        print(f"  {label:30}{weight:>+10.3f}")
    print()
    print("Measured on the 22 real incidents, which the model never sees:")
    print("  Synchronous+critical 85.7% | Synchronous 50% | Data 50% | Asynchronous 40%")


if __name__ == "__main__":
    main()
