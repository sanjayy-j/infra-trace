"""
Training loop shared by SCP-GNN and the neural baselines.

All models in this package expose the same call signature -
`model(x, src, dst, edge_attr, roots) -> (B, N) probabilities` - so exactly
one training procedure is used for every architecture. Any difference in the
reported results is therefore a difference in the model, not in how it was
optimised. The alternative, hand-tuning each architecture separately, makes
the comparison impossible to interpret.

LOSS
----
Binary cross-entropy over the evaluation mask, with `pos_weight` set from
the training split's class balance. Blast radii are sparse - most components
are unaffected by most incidents - and an unweighted BCE converges happily
to predicting "nothing is affected".

By default the mask covers only components reachable from the root.
Unreachable components are structurally guaranteed to be zero by SCP-GNN's
propagation rule, so including them hands the model a large pile of free
true negatives that it cannot get wrong, inflating every metric and
contributing no gradient. The `reach_mask` flag turns this off so the effect
can be measured rather than assumed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import torch
from torch import Tensor

from .graph import Cascade, Graph

Dataset = list[tuple[Graph, list[Cascade]]]


@dataclass
class TrainConfig:
    epochs: int = 120
    lr: float = 0.01
    weight_decay: float = 1e-4
    patience: int = 20          # early stopping on validation PR-AUC
    reach_mask: bool = True
    # Score and train over the bidirectional candidate set. Set for every
    # model in the directionality experiment, including the reverse-only
    # ones, so they are all judged on the same question.
    bidirectional_mask: bool = False
    # Feed the model the bidirectional edge set (SCP-BiD only).
    bidirectional_edges: bool = False
    seed: int = 0
    verbose: bool = False
    # Upweighting positives raises F1 but systematically inflates predicted
    # probabilities, which costs calibration. Both settings are measured in
    # the benchmark rather than assumed; see docs/ml/experiments.md.
    use_pos_weight: bool = True


def to_tensors(
    graph: Graph, device: str = "cpu", bidirectional: bool = False
) -> dict[str, Tensor]:
    """Convert a Graph into the tensors every model consumes.

    `bidirectional` selects the two-direction edge set used by SCP-BiD, with
    the `is_forward` flag appended to each edge's features.
    """
    if bidirectional:
        from .models.scp_bid import bidirectional_tensors

        return bidirectional_tensors(graph, device)
    return {
        "x": torch.as_tensor(graph.node_features, dtype=torch.float32, device=device),
        "src": torch.as_tensor(graph.src, dtype=torch.long, device=device),
        "dst": torch.as_tensor(graph.dst, dtype=torch.long, device=device),
        "edge_attr": torch.as_tensor(
            graph.edge_features, dtype=torch.float32, device=device
        ),
    }


def _batch(
    graph: Graph,
    cascades: list[Cascade],
    reach_mask: bool,
    device: str,
    bidirectional_mask: bool = False,
):
    """Stack one graph's cascades into (B, N) label and mask tensors."""
    roots = torch.as_tensor(
        [c.root_index for c in cascades], dtype=torch.long, device=device
    )
    labels = torch.as_tensor(
        np.stack([c.labels for c in cascades]), dtype=torch.float32, device=device
    )
    masks = torch.as_tensor(
        np.stack(
            [
                c.eval_mask(
                    graph,
                    reachable_only=reach_mask,
                    bidirectional=bidirectional_mask,
                )
                for c in cascades
            ]
        ),
        dtype=torch.bool,
        device=device,
    )
    return roots, labels, masks


def _pos_weight(
    dataset: Dataset, reach_mask: bool, bidirectional_mask: bool = False
) -> float:
    """Ratio of negatives to positives over the masked training entries."""
    pos = neg = 0
    for graph, cascades in dataset:
        for cas in cascades:
            mask = cas.eval_mask(
                graph, reachable_only=reach_mask, bidirectional=bidirectional_mask
            )
            y = cas.labels[mask]
            pos += int((y > 0.5).sum())
            neg += int((y <= 0.5).sum())
    if pos == 0:
        return 1.0
    return float(np.clip(neg / pos, 0.2, 20.0))


def pr_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Average precision. Returns nan when a split has only one class."""
    from sklearn.metrics import average_precision_score

    if y_true.size == 0 or len(np.unique(y_true)) < 2:
        return float("nan")
    return float(average_precision_score(y_true, y_score))


@torch.no_grad()
def collect_predictions(
    model,
    dataset: Dataset,
    reach_mask: bool = True,
    device: str = "cpu",
    bidirectional_mask: bool = False,
    bidirectional_edges: bool = False,
    **forward_kwargs,
) -> tuple[np.ndarray, np.ndarray]:
    """Run a torch model over a dataset, returning flat (y_true, y_score)."""
    model.eval()
    ys, ps = [], []
    for graph, cascades in dataset:
        if not cascades:
            continue
        t = to_tensors(graph, device, bidirectional_edges)
        roots, labels, masks = _batch(
            graph, cascades, reach_mask, device, bidirectional_mask
        )
        out = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots, **forward_kwargs)
        ys.append(labels[masks].cpu().numpy())
        ps.append(out[masks].cpu().numpy())
    if not ys:
        return np.array([]), np.array([])
    return np.concatenate(ys), np.concatenate(ps)


def collect_predictions_classical(
    model, dataset: Dataset, reach_mask: bool = True, bidirectional_mask: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """Same, for the non-torch baselines (reachability, depth decay, logistic)."""
    ys, ps = [], []
    for graph, cascades in dataset:
        for cas in cascades:
            mask = cas.eval_mask(
                graph, reachable_only=reach_mask, bidirectional=bidirectional_mask
            )
            if not mask.any():
                continue
            ys.append(cas.labels[mask])
            ps.append(model.predict(graph, cas.root_index)[mask])
    if not ys:
        return np.array([]), np.array([])
    return np.concatenate(ys), np.concatenate(ps)


def train_model(
    model,
    train_ds: Dataset,
    val_ds: Dataset,
    cfg: TrainConfig | None = None,
    device: str = "cpu",
    **forward_kwargs,
) -> dict:
    """Fit `model`, keeping the parameters with the best validation PR-AUC.

    Returns a history dict with the best score, the epoch it occurred at and
    the wall-clock training time.
    """
    cfg = cfg or TrainConfig()
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    model.to(device)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    pos_weight = torch.tensor(
        _pos_weight(train_ds, cfg.reach_mask, cfg.bidirectional_mask)
        if cfg.use_pos_weight
        else 1.0,
        device=device,
    )
    loss_fn = torch.nn.BCELoss(reduction="none")

    best_score, best_state, best_epoch = -float("inf"), None, -1
    since_improved = 0
    started = time.perf_counter()

    # Pre-build tensors once; the graphs do not change between epochs.
    prepared = [
        (
            to_tensors(g, device, cfg.bidirectional_edges),
            *_batch(g, c, cfg.reach_mask, device, cfg.bidirectional_mask),
        )
        for g, c in train_ds
        if c
    ]

    for epoch in range(cfg.epochs):
        model.train()
        total_loss, total_n = 0.0, 0
        for t, roots, labels, masks in prepared:
            if not masks.any():
                continue
            opt.zero_grad()
            out = model(
                t["x"], t["src"], t["dst"], t["edge_attr"], roots, **forward_kwargs
            )
            # Weight positives up; BCELoss has no pos_weight argument, so the
            # weighting is applied by hand to keep the probability-valued
            # output (BCEWithLogits would require models to emit logits, and
            # SCP-GNN's output is a probability by construction).
            raw = loss_fn(out.clamp(1e-6, 1 - 1e-6), labels)
            weights = torch.where(labels > 0.5, pos_weight, torch.ones_like(labels))
            loss = (raw * weights)[masks].mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            total_loss += float(loss.detach()) * int(masks.sum())
            total_n += int(masks.sum())

        y, p = collect_predictions(
            model,
            val_ds,
            cfg.reach_mask,
            device,
            cfg.bidirectional_mask,
            cfg.bidirectional_edges,
            **forward_kwargs,
        )
        score = pr_auc(y, p)
        score = -float("inf") if np.isnan(score) else score

        if score > best_score:
            best_score, best_epoch = score, epoch
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            since_improved = 0
        else:
            since_improved += 1
            if since_improved >= cfg.patience:
                break

        if cfg.verbose and epoch % 10 == 0:
            mean_loss = total_loss / max(total_n, 1)
            print(f"  epoch {epoch:3d}  loss {mean_loss:.4f}  val PR-AUC {score:.4f}")

    if best_state is not None:
        model.load_state_dict(best_state)

    return {
        "best_val_pr_auc": best_score,
        "best_epoch": best_epoch,
        "train_seconds": time.perf_counter() - started,
    }
