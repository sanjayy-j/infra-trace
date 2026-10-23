"""
SCP-Latent - cascade propagation over observed plus inferred dependencies.

THE PROBLEM IT ADDRESSES
------------------------
SCP-GNN assigns probability EXACTLY ZERO to any component unreachable from
the root (proved in docs/ml/design.md 4.2). On a complete graph that is a
safety property. On an incomplete one it is a cliff: a dependency nobody wrote
down becomes a confident zero, and the model has no way to say "I see no path,
but these two look like the sort of pair that usually has one".

The InfraTrace dependency table is hand-maintained and we can point at a
specific omission: incident 22 names a shipment-status cache, Redis Cache is
that cache, and no dependency row records the link. Removing just three of the
29 real edges costs almost six points of attainable recall.

THE MECHANISM
-------------
Keep SCP-GNN exactly, and admit a second, low-probability edge set.

For an ordered non-adjacent pair (s, t), score a LATENT dependency from node
features alone:

    l_st = sigmoid( MLP_phi( [ x_s ; x_t ] ) )

Keep the top-k per destination and give them transmission

    p_st = l_st * sigmoid( MLP_theta( [ x_s ; x_t ; 0 ; 1 ] ) )

where the zeros stand in for edge features we do not have (an unrecorded
dependency has no recorded type) and the trailing 1 marks the edge as latent.
Observed edges keep their real features, a latent flag of 0, and l = 1, so
with top_k = 0 this reduces EXACTLY to SCP-GNN.

Composition is unchanged: the same noisy-OR fixed point. The product form
matters here - a latent edge with l = 0.1 and p = 0.3 contributes a factor of
(1 - 0.03), a small hedge rather than a hard zero, which is the whole point.

NOT NOVEL
---------
Scoring candidate edges from node features and adding them to the graph is the
standard move in graph structure learning: LDS (arXiv:1903.11960),
Graph-Revised Convolutional Network (arXiv:1911.07123), GDN
(arXiv:2205.09575), survey arXiv:2103.03036. The contribution here, if any, is
empirical - see docs/ml/incomplete-graph-study.md. No novelty is claimed.

THE PREDICTION THIS IS TESTING
------------------------------
Training-time edge dropout (DropEdge, arXiv:1907.10903) CANNOT fix a
structural zero. It changes what the model learns, not the fact that at
inference a node with no path has every noisy-OR factor equal to 1 and an
output of exactly 0. Only adding candidate edges can reach those components.
SCP-Latent exists to test that claim; DropEdge is run alongside it as the
control.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
from torch import Tensor, nn

from ..graph import EDGE_FEATURE_DIM, Graph
from ..incomplete import latent_candidate_pairs
from .scp import EPS, SCPConfig, SCPNet

# Observed edge features plus one flag marking an edge as latent.
LATENT_EDGE_FEATURE_DIM = EDGE_FEATURE_DIM + 1


@dataclass
class SCPLatentConfig:
    node_dim: int
    edge_dim: int = LATENT_EDGE_FEATURE_DIM
    hidden: int = 32
    rounds: int = 4
    dropout: float = 0.1
    linear_link: bool = False
    # Latent edges admitted per destination node. 0 reduces the model to
    # SCP-GNN exactly, which the test suite asserts.
    top_k: int = 2
    # Ablation: score candidates at random instead of learning l_st. Isolates
    # whether the LEARNED scorer matters or merely having extra candidate
    # edges does - the same trap SCP-BiD fell into.
    random_scoring: bool = False


class LatentEdgeScorer(nn.Module):
    """Plausibility that an unrecorded dependency exists between two nodes."""

    def __init__(self, node_dim: int, hidden: int, dropout: float) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(2 * node_dim, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, 1),
        )

    def forward(self, x: Tensor, src: Tensor, dst: Tensor) -> Tensor:
        if src.numel() == 0:
            return torch.zeros(0, device=x.device, dtype=x.dtype)
        return torch.sigmoid(
            self.net(torch.cat([x[src], x[dst]], dim=-1)).squeeze(-1)
        ).clamp(EPS, 1.0 - EPS)


class SCPLatentNet(SCPNet):
    """SCP-GNN over observed edges plus top-k inferred latent edges.

    Subclasses SCPNet so the propagation fixed point is literally the same
    code. If this class held its own copy, a later change to one would
    silently diverge and the comparison between them would stop being a
    comparison of the latent-edge mechanism alone.
    """

    def __init__(self, cfg: SCPLatentConfig) -> None:
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
        self.lat_cfg = cfg
        self.scorer = LatentEdgeScorer(cfg.node_dim, cfg.hidden, cfg.dropout)

    # -----------------------------------------------------------------
    def select_latent(
        self, x: Tensor, cand_src: Tensor, cand_dst: Tensor, num_nodes: int
    ) -> tuple[Tensor, Tensor, Tensor]:
        """Score candidates and keep the top-k per destination.

        Hard top-k selection is not differentiable through the choice itself,
        which is standard for kNN-style graph construction. Gradients still
        flow through the scores of the edges that were selected, which is what
        trains the scorer.
        """
        if cand_src.numel() == 0 or self.lat_cfg.top_k <= 0:
            empty = torch.zeros(0, dtype=torch.long, device=x.device)
            return empty, empty, torch.zeros(0, device=x.device, dtype=x.dtype)

        if self.lat_cfg.random_scoring:
            scores = torch.rand(cand_src.shape[0], device=x.device, dtype=x.dtype)
            gate = torch.full_like(scores, 0.5)
        else:
            scores = self.scorer(x, cand_src, cand_dst)
            gate = scores

        keep: list[int] = []
        dst_np = cand_dst.detach().cpu().numpy()
        score_np = scores.detach().cpu().numpy()
        for node in range(num_nodes):
            idx = np.nonzero(dst_np == node)[0]
            if idx.size == 0:
                continue
            k = min(self.lat_cfg.top_k, idx.size)
            keep.extend(idx[np.argsort(-score_np[idx])[:k]].tolist())

        if not keep:
            empty = torch.zeros(0, dtype=torch.long, device=x.device)
            return empty, empty, torch.zeros(0, device=x.device, dtype=x.dtype)

        sel = torch.as_tensor(sorted(keep), dtype=torch.long, device=x.device)
        return cand_src[sel], cand_dst[sel], gate[sel]

    # -----------------------------------------------------------------
    def build_edges(
        self,
        x: Tensor,
        src: Tensor,
        dst: Tensor,
        edge_attr: Tensor,
        cand_src: Tensor,
        cand_dst: Tensor,
    ):
        """Combine observed and selected latent edges into one edge set.

        Returns (src, dst, edge_attr, gate) where `gate` multiplies the
        transmission probability: 1 for observed edges, l_st for latent ones.
        """
        l_src, l_dst, l_gate = self.select_latent(x, cand_src, cand_dst, x.shape[0])
        obs_gate = torch.ones(src.shape[0], device=x.device, dtype=x.dtype)
        if l_src.numel() == 0:
            return src, dst, edge_attr, obs_gate

        # Latent edges carry zeroed edge features - an unrecorded dependency
        # has no recorded type - and a trailing flag of 1.
        l_attr = torch.zeros(
            l_src.shape[0], edge_attr.shape[1], device=x.device, dtype=edge_attr.dtype
        )
        l_attr[:, -1] = 1.0
        return (
            torch.cat([src, l_src]),
            torch.cat([dst, l_dst]),
            torch.cat([edge_attr, l_attr], dim=0),
            torch.cat([obs_gate, l_gate]),
        )

    # -----------------------------------------------------------------
    def forward(
        self,
        x: Tensor,
        src: Tensor,
        dst: Tensor,
        edge_attr: Tensor,
        roots: Tensor,
        cand_src: Tensor | None = None,
        cand_dst: Tensor | None = None,
        rounds: int | None = None,
        **kwargs,
    ) -> Tensor:
        """Propagate over observed plus latent edges.

        Reimplements the noisy-OR loop rather than calling super().forward()
        only because the per-edge probability has to be multiplied by the
        latent gate before composition; the recursion itself is identical to
        ml/models/scp.py and is kept line-for-line comparable.
        """
        if cand_src is None or cand_dst is None:
            return super().forward(x, src, dst, edge_attr, roots, rounds=rounds)

        e_src, e_dst, e_attr, gate = self.build_edges(
            x, src, dst, edge_attr, cand_src, cand_dst
        )
        p = self.edge_probabilities(x, e_src, e_dst, e_attr) * gate
        p = p.clamp(EPS, 1.0 - EPS)

        num_nodes = x.shape[0]
        batch = roots.shape[0]
        k_rounds = self.cfg.rounds if rounds is None else rounds

        log_surv = torch.zeros(batch, num_nodes, device=x.device, dtype=p.dtype)
        root_onehot = torch.zeros_like(log_surv)
        root_onehot[torch.arange(batch, device=x.device), roots] = 1.0
        log_surv = log_surv.masked_fill(root_onehot.bool(), -30.0)

        p_e = p.unsqueeze(0)
        for _ in range(k_rounds):
            q = (1.0 - torch.exp(log_surv)).clamp(0.0, 1.0 - EPS)
            contrib = torch.log1p(-(p_e * q[:, e_src]).clamp(max=1.0 - EPS))
            agg = torch.zeros_like(log_surv).index_add_(1, e_dst, contrib)
            log_surv = torch.minimum(log_surv, agg)
            log_surv = log_surv.masked_fill(root_onehot.bool(), -30.0)

        q = (1.0 - torch.exp(log_surv)).clamp(EPS, 1.0)
        return q.masked_fill(root_onehot.bool(), 1.0)


def latent_tensors(graph: Graph, device: str = "cpu") -> dict[str, Tensor]:
    """Tensors for SCP-Latent: observed edges with a latent flag, plus candidates."""
    num_edges = graph.num_edges
    attr = np.concatenate(
        [graph.edge_features, np.zeros((num_edges, 1), dtype=np.float32)], axis=1
    )
    cand_src, cand_dst = latent_candidate_pairs(graph, top_k=0)
    return {
        "x": torch.as_tensor(graph.node_features, dtype=torch.float32, device=device),
        "src": torch.as_tensor(graph.src, dtype=torch.long, device=device),
        "dst": torch.as_tensor(graph.dst, dtype=torch.long, device=device),
        "edge_attr": torch.as_tensor(attr, dtype=torch.float32, device=device),
        "cand_src": torch.as_tensor(cand_src, dtype=torch.long, device=device),
        "cand_dst": torch.as_tensor(cand_dst, dtype=torch.long, device=device),
    }
