"""
SCP-BiD - bidirectional Semantic Cascade Propagation.

WHAT IS DIFFERENT FROM SCP-GNN, EXACTLY
---------------------------------------
One input feature.

SCP-GNN propagates failure only along REVERSE dependency edges: a dependency
fails, its dependents suffer. That is what the recursive CTE models. Measured
on the 22 recorded incidents, it leaves 7 of 37 impacts structurally
unreachable, because those failures travelled FORWARDS - a component damaging
something it depends on, through a retry storm, a cache it churned, or bad
messages it produced.

SCP-BiD gives every dependency row two propagation edges instead of one, and
appends a binary `is_forward` flag to the edge features:

    reverse   v -> u    flag 0      (u depends on v)
    forward   u -> v    flag 1

    p_e = sigma( MLP_theta( [ x_s ; x_t ; z_e ; dir_e ] ) )

The composition rule, the loss, the training loop and the architecture are
untouched. A single shared transmission function now learns different
probabilities for the two directions, because it can see which one it is
looking at. This is deliberately the smallest change that could work: the
point is to measure whether directionality matters, not to add capacity.

THE MECHANISM IS NOT NOVEL
--------------------------
Separate treatment of incoming and outgoing edges is Dir-GNN (Rossi et al.
2023, arXiv:2305.10498), and modelling inverse relations as distinct relation
types is R-GCN (Schlichtkrull et al. 2018) and CompGCN (arXiv:1911.03082).
Forward failure propagation is studied in the systems literature as retry
amplification (arXiv:2608.25403). Any contribution here is the measurement -
forward edges carry failure at 31.8% against 61.9% for reverse ones on real
incidents - not the architecture. See docs/ml/direction-study.md.

WHAT THIS COSTS
---------------
SCP-GNN could never flag a component the SQL would not; that safety property
is why it was safe to ship. SCP-BiD gives it up on purpose. Its bound is the
BIDIRECTIONAL reachable set, roughly six times larger on the real graph (396
pairs against 66). Recall that reverse traversal cannot reach is bought with
a much bigger candidate set, and whether that trade is worth making is the
empirical question, not an assumption.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
from torch import Tensor

from ..graph import BI_EDGE_FEATURE_DIM, Graph
from .scp import SCPConfig, SCPNet


@dataclass
class SCPBiDConfig:
    """Mirrors SCPConfig, with the direction flag as the one addition."""

    node_dim: int
    edge_dim: int = BI_EDGE_FEATURE_DIM
    hidden: int = 32
    rounds: int = 4
    dropout: float = 0.1
    linear_link: bool = False
    # Ablation. False zeroes the `is_forward` column, so the model still sees
    # both directions' edges but cannot tell them apart. This is the
    # load-bearing comparison: it separates "directionality matters" from
    # "more edges help", which comparing against reverse-only SCP-GNN alone
    # would conflate.
    use_direction_flag: bool = True


class SCPBiDNet(SCPNet):
    """SCP-GNN over the bidirectional edge set.

    Subclasses SCPNet rather than reimplementing it, because the propagation
    mathematics is genuinely identical. If this class held its own copy of the
    fixed point, a later change to one would silently diverge from the other
    and the comparison between them would stop being a comparison of one
    feature.
    """

    def __init__(self, cfg: SCPBiDConfig) -> None:
        super().__init__(
            SCPConfig(
                node_dim=cfg.node_dim,
                edge_dim=cfg.edge_dim,
                hidden=cfg.hidden,
                rounds=cfg.rounds,
                dropout=cfg.dropout,
                linear_link=cfg.linear_link,
            )
        )
        self.bid_cfg = cfg

    def _maybe_blind(self, edge_attr: Tensor) -> Tensor:
        """Zero the direction column when running the direction-blind ablation."""
        if self.bid_cfg.use_direction_flag:
            return edge_attr
        blinded = edge_attr.clone()
        blinded[:, -1] = 0.0
        return blinded

    def edge_probabilities(self, x, src, dst, edge_attr) -> Tensor:
        return super().edge_probabilities(x, src, dst, self._maybe_blind(edge_attr))

    def forward(self, x, src, dst, edge_attr, roots, **kwargs) -> Tensor:
        return super().forward(
            x, src, dst, self._maybe_blind(edge_attr), roots, **kwargs
        )


def bidirectional_tensors(graph: Graph, device: str = "cpu") -> dict[str, Tensor]:
    """Tensors for the bidirectional edge set, in the shape models expect."""
    if graph.bi_src is None:
        raise ValueError(
            "Graph has no bidirectional view. It was built before ml/graph.py "
            "gained the bi_* arrays; rebuild it with from_edges()."
        )
    return {
        "x": torch.as_tensor(graph.node_features, dtype=torch.float32, device=device),
        "src": torch.as_tensor(graph.bi_src, dtype=torch.long, device=device),
        "dst": torch.as_tensor(graph.bi_dst, dtype=torch.long, device=device),
        "edge_attr": torch.as_tensor(
            graph.bi_edge_features, dtype=torch.float32, device=device
        ),
    }
