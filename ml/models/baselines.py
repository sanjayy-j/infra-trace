"""
Baselines for blast-radius prediction.

FAIRNESS
--------
Every neural baseline receives exactly the same inputs as SCP-GNN: node
features, the root indicator, and the edge features (dependency type and
criticality) concatenated into each message. Standard GCN ignores edge
attributes; withholding them here would mean any measured gain for SCP-GNN
could be explained by "it saw the dependency type and the baseline did not",
which would say nothing about the composition rule. They are therefore
supplied to all of them, and the only remaining difference is architecture.

The baselines are implemented directly on scatter operations rather than
with torch-geometric. The graphs are small, the layers are short, and adding
a heavyweight geometric-deep-learning dependency to a project whose other
requirements are four pure-Python packages is not justified by what it would
save.

WHAT EACH ONE IS FOR
--------------------
ReachabilityBaseline   The system as it stands today - the recursive CTE in
                       database/queries/07_blast_radius.sql. This is the
                       number to beat; everything else is context.
DepthDecayBaseline     The obvious non-ML fix: trust nearby components more.
                       One fitted parameter. If a GNN cannot beat this, the
                       GNN is not earning its complexity.
LogisticBaseline       Hand-engineered path features + logistic regression.
                       Tests whether the problem needs message passing at all.
GCNNet / SAGENet /     Conventional architectures with learned embeddings and
GATNet                 a classifier head, i.e. the standard way this task
                       would be approached.
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor, nn

from ..graph import Cascade, Graph

EPS = 1e-6


# =====================================================================
# Non-learned and shallow baselines
# =====================================================================
class ReachabilityBaseline:
    """Predict 1.0 for every component reachable from the root, else 0.

    This is precisely what the production SQL does. It is a sound upper
    bound on the true blast radius under the assumption that any dependency
    can carry a failure.
    """

    name = "reachability_sql"

    def predict(self, graph: Graph, root_index: int) -> np.ndarray:
        return graph.reachable_from(root_index).astype(np.float64)


class BidirectionalReachabilityBaseline:
    """Predict 1.0 for every component reachable by edges in EITHER direction.

    The structural upper bound available to SCP-BiD, and the thing it has to
    beat. On the real incidents it reaches 100% recall at 9.3% precision - it
    finds everything and is useless, which is precisely why a learned
    transmission function is needed on top of it.
    """

    name = "reachability_bidirectional"

    def predict(self, graph: Graph, root_index: int) -> np.ndarray:
        return graph.reachable_bidirectional(root_index).astype(np.float64)


class DepthDecayBaseline:
    """Predict lambda^depth for reachable components, 0 otherwise.

    One parameter, fitted by scanning a grid on the training split. Encodes
    the intuition that failure attenuates with distance without learning
    anything about *why*.
    """

    name = "depth_decay"

    def __init__(self, lam: float = 0.6) -> None:
        self.lam = lam

    def fit(self, dataset: list[tuple[Graph, list[Cascade]]]) -> "DepthDecayBaseline":
        best_lam, best_loss = self.lam, float("inf")
        for lam in np.linspace(0.05, 0.98, 32):
            total, count = 0.0, 0
            for graph, cascades in dataset:
                for cas in cascades:
                    mask = cas.eval_mask(graph)
                    if not mask.any():
                        continue
                    depth = graph.hop_distance_from(cas.root_index)
                    pred = np.where(depth > 0, lam ** np.maximum(depth, 0), 0.0)
                    y = cas.labels[mask]
                    p = np.clip(pred[mask], EPS, 1 - EPS)
                    total += float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).sum())
                    count += int(mask.sum())
            if count and total / count < best_loss:
                best_loss, best_lam = total / count, float(lam)
        self.lam = best_lam
        return self

    def predict(self, graph: Graph, root_index: int) -> np.ndarray:
        depth = graph.hop_distance_from(root_index)
        return np.where(depth > 0, self.lam ** np.maximum(depth, 0), 0.0)


def path_features(
    graph: Graph, root_index: int, bidirectional: bool = False
) -> np.ndarray:
    """Hand-engineered per-node features describing the route from the root.

    Used by LogisticBaseline. Captures what an engineer would reach for
    first: how far away the component is, how many ways the failure could
    get to it, and what kind of dependency the last hop was.
    """
    n = graph.num_nodes
    if bidirectional and graph.bi_src is not None:
        # Same features, computed over the bidirectional edge set, so the
        # logistic baseline can see forward-reachable candidates at all. A
        # reverse-only feature set would score every forward candidate 0 and
        # the comparison against SCP-BiD would be meaningless.
        reach = graph.reachable_bidirectional(root_index)
        e_src, e_dst = graph.bi_src, graph.bi_dst
        e_feat = graph.bi_edge_features
        depth = _bidirectional_hops(graph, root_index)
    else:
        reach = graph.reachable_from(root_index)
        e_src, e_dst, e_feat = graph.src, graph.dst, graph.edge_features
        depth = graph.hop_distance_from(root_index)

    # Number of incoming edges from components that are themselves reachable
    # (or are the root) - how many distinct routes failure could arrive by.
    routes = np.zeros(n)
    best_edge = np.zeros((n, e_feat.shape[1]))
    for e in range(e_src.shape[0]):
        s, d = int(e_src[e]), int(e_dst[e])
        if s == root_index or reach[s]:
            routes[d] += 1.0
            # Keep the "strongest" incoming edge: critical beats non-critical.
            if e_feat[e][-1] >= best_edge[d][-1]:
                best_edge[d] = e_feat[e]

    feats = np.column_stack(
        [
            reach.astype(float),
            np.where(depth > 0, depth, 0.0),
            np.where(depth > 0, 1.0 / np.maximum(depth, 1), 0.0),
            np.log1p(routes),
            best_edge,
            graph.node_features,
        ]
    )
    return feats


def _bidirectional_hops(graph: Graph, root_index: int) -> np.ndarray:
    """Shortest hop count over the bidirectional edge set; -1 if unreachable."""
    dist = np.full(graph.num_nodes, -1, dtype=np.int64)
    dist[root_index] = 0
    adj: dict[int, list[int]] = {}
    for e in range(graph.bi_src.shape[0]):
        adj.setdefault(int(graph.bi_src[e]), []).append(int(graph.bi_dst[e]))
    frontier = [root_index]
    while frontier:
        nxt = []
        for node in frontier:
            for nbr in adj.get(node, ()):
                if dist[nbr] == -1:
                    dist[nbr] = dist[node] + 1
                    nxt.append(nbr)
        frontier = nxt
    return dist


class LogisticBaseline:
    """Logistic regression on `path_features`. No message passing."""

    name = "logistic_path_features"

    def __init__(self, bidirectional: bool = False) -> None:
        from sklearn.linear_model import LogisticRegression

        self.model = LogisticRegression(max_iter=2000, C=1.0)
        self._fitted = False
        self.bidirectional = bidirectional

    def fit(self, dataset: list[tuple[Graph, list[Cascade]]]) -> "LogisticBaseline":
        xs, ys = [], []
        for graph, cascades in dataset:
            for cas in cascades:
                mask = cas.eval_mask(graph, bidirectional=self.bidirectional)
                if not mask.any():
                    continue
                xs.append(
                    path_features(graph, cas.root_index, self.bidirectional)[mask]
                )
                ys.append(cas.labels[mask])
        if not xs:
            return self
        x = np.vstack(xs)
        y = np.concatenate(ys)
        if len(np.unique(y)) < 2:
            return self
        self.model.fit(x, y)
        self._fitted = True
        return self

    def predict(self, graph: Graph, root_index: int) -> np.ndarray:
        feats = path_features(graph, root_index, self.bidirectional)
        if not self._fitted:
            return np.zeros(graph.num_nodes)
        prob = self.model.predict_proba(feats)[:, 1]
        # Respect the structural bound: an unreachable component cannot be
        # affected by this root cause. Without this the baseline would be
        # penalised for something the graph already rules out, which would
        # flatter the proposed model unfairly.
        bound = (
            graph.reachable_bidirectional(root_index)
            if self.bidirectional
            else graph.reachable_from(root_index)
        )
        return prob * bound


# =====================================================================
# Neural baselines
# =====================================================================
class _MessagePassingBase(nn.Module):
    """Shared plumbing: root indicator, edge-aware messages, classifier head."""

    def __init__(self, node_dim: int, edge_dim: int, hidden: int, layers: int,
                 dropout: float) -> None:
        super().__init__()
        self.in_dim = node_dim + 1  # + root indicator
        self.edge_dim = edge_dim
        self.hidden = hidden
        self.layers = layers
        self.dropout = nn.Dropout(dropout)
        self.encoder = nn.Linear(self.in_dim, hidden)
        self.head = nn.Sequential(
            nn.Linear(hidden, hidden // 2), nn.ReLU(), nn.Linear(hidden // 2, 1)
        )

    def _input(self, x: Tensor, roots: Tensor) -> Tensor:
        """(B, N, hidden) initial node states with the root flagged."""
        batch, num_nodes = roots.shape[0], x.shape[0]
        root_flag = torch.zeros(batch, num_nodes, 1, device=x.device, dtype=x.dtype)
        root_flag[torch.arange(batch, device=x.device), roots, 0] = 1.0
        xb = x.unsqueeze(0).expand(batch, -1, -1)
        return torch.relu(self.encoder(torch.cat([xb, root_flag], dim=-1)))

    def forward(self, x, src, dst, edge_attr, roots, **_) -> Tensor:
        h = self._input(x, roots)
        for layer in self.convs:  # type: ignore[attr-defined]
            h = torch.relu(layer(h, src, dst, edge_attr))
            h = self.dropout(h)
        return torch.sigmoid(self.head(h).squeeze(-1)).clamp(EPS, 1 - EPS)


class _GCNLayer(nn.Module):
    """Mean aggregation with a symmetric-style degree normalisation."""

    def __init__(self, hidden: int, edge_dim: int) -> None:
        super().__init__()
        self.msg = nn.Linear(hidden + edge_dim, hidden)
        self.upd = nn.Linear(2 * hidden, hidden)

    def forward(self, h: Tensor, src: Tensor, dst: Tensor, edge_attr: Tensor) -> Tensor:
        batch, num_nodes, _ = h.shape
        ea = edge_attr.unsqueeze(0).expand(batch, -1, -1)
        m = self.msg(torch.cat([h[:, src], ea], dim=-1))
        agg = torch.zeros_like(h).index_add_(1, dst, m)
        deg = torch.zeros(num_nodes, device=h.device, dtype=h.dtype).index_add_(
            0, dst, torch.ones(src.shape[0], device=h.device, dtype=h.dtype)
        ).clamp(min=1.0)
        return self.upd(torch.cat([h, agg / deg.view(1, -1, 1)], dim=-1))


class _SAGELayer(nn.Module):
    """GraphSAGE: concatenate self state with the neighbourhood mean."""

    def __init__(self, hidden: int, edge_dim: int) -> None:
        super().__init__()
        self.msg = nn.Linear(hidden + edge_dim, hidden)
        self.upd = nn.Linear(2 * hidden, hidden)

    def forward(self, h: Tensor, src: Tensor, dst: Tensor, edge_attr: Tensor) -> Tensor:
        batch, num_nodes, _ = h.shape
        ea = edge_attr.unsqueeze(0).expand(batch, -1, -1)
        m = torch.relu(self.msg(torch.cat([h[:, src], ea], dim=-1)))
        agg = torch.zeros_like(h).index_add_(1, dst, m)
        deg = torch.zeros(num_nodes, device=h.device, dtype=h.dtype).index_add_(
            0, dst, torch.ones(src.shape[0], device=h.device, dtype=h.dtype)
        ).clamp(min=1.0)
        return self.upd(torch.cat([h, agg / deg.view(1, -1, 1)], dim=-1))


class _GATLayer(nn.Module):
    """Single-head additive attention over incoming edges (Velickovic et al.)."""

    def __init__(self, hidden: int, edge_dim: int) -> None:
        super().__init__()
        self.proj = nn.Linear(hidden, hidden)
        self.att = nn.Linear(2 * hidden + edge_dim, 1)
        self.upd = nn.Linear(2 * hidden, hidden)

    def forward(self, h: Tensor, src: Tensor, dst: Tensor, edge_attr: Tensor) -> Tensor:
        batch, num_nodes, _ = h.shape
        hp = self.proj(h)
        ea = edge_attr.unsqueeze(0).expand(batch, -1, -1)
        score = F.leaky_relu(
            self.att(torch.cat([hp[:, src], hp[:, dst], ea], dim=-1)).squeeze(-1), 0.2
        )
        # Softmax over each node's incoming edges, done with scatter ops:
        # subtract a per-destination max for stability, exponentiate, then
        # normalise by the per-destination sum.
        max_per_dst = torch.full(
            (batch, num_nodes), -1e30, device=h.device, dtype=h.dtype
        ).index_reduce_(1, dst, score, "amax", include_self=True)
        ex = torch.exp(score - max_per_dst[:, dst])
        denom = torch.zeros(batch, num_nodes, device=h.device, dtype=h.dtype).index_add_(
            1, dst, ex
        ).clamp(min=EPS)
        alpha = (ex / denom[:, dst]).unsqueeze(-1)
        agg = torch.zeros_like(h).index_add_(1, dst, alpha * hp[:, src])
        return self.upd(torch.cat([h, agg], dim=-1))


def _make(cls, node_dim: int, edge_dim: int, hidden: int, layers: int, dropout: float):
    model = _MessagePassingBase(node_dim, edge_dim, hidden, layers, dropout)
    model.convs = nn.ModuleList([cls(hidden, edge_dim) for _ in range(layers)])
    return model


def GCNNet(node_dim: int, edge_dim: int, hidden: int = 32, layers: int = 4,
           dropout: float = 0.1):
    return _make(_GCNLayer, node_dim, edge_dim, hidden, layers, dropout)


def SAGENet(node_dim: int, edge_dim: int, hidden: int = 32, layers: int = 4,
            dropout: float = 0.1):
    return _make(_SAGELayer, node_dim, edge_dim, hidden, layers, dropout)


def GATNet(node_dim: int, edge_dim: int, hidden: int = 32, layers: int = 4,
           dropout: float = 0.1):
    return _make(_GATLayer, node_dim, edge_dim, hidden, layers, dropout)
