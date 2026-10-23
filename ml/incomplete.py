"""
Hiding edges from a dependency graph, to study incomplete-graph robustness.

WHY THE GROUND TRUTH MUST COME FROM THE COMPLETE GRAPH
------------------------------------------------------
Cascades are simulated on the FULL graph and only then are edges hidden from
the model's input. The labels therefore describe what really happened,
including propagation through edges the model cannot see. Hiding edges before
simulating would change the ground truth itself and the experiment would
measure nothing: the model would be scored against a world where the missing
dependency genuinely does not exist.

TWO REMOVAL REGIMES, AND WHY BOTH ARE NEEDED
--------------------------------------------
Chandrasekhar et al. (2024, arXiv:2403.05704) prove that the GEOMETRY of the
missingness decides the damage. Uniformly random thinning preserves structure
and diffusion estimates stay comparatively robust; missing links that BRIDGE
distant parts of the network act as shortcuts and cause catastrophic forecast
failure. A study that only removed edges at random would be testing the case
the theory already says is benign.

  random  uniform thinning                           - the benign case
  bridge  highest DIRECTED edge-betweenness first    - the catastrophic case

Betweenness is computed on the DIRECTED graph. Failure travels along directed
paths, so an edge may lie on many undirected shortest paths and carry no
directed ones; targeting by undirected betweenness turned out to be no worse
than random removal. See the note on `_edge_betweenness_order`.
"""

from __future__ import annotations

import numpy as np

from .graph import (
    COMPONENT_TYPES,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    Graph,
)

# Node feature layout from ml/graph.py: the last two columns are the
# normalised in/out degrees, and they are the only ones that change when
# edges are hidden.
_DEGREE_COLS = 2
_IN_DEG_COL = -2
_OUT_DEG_COL = -1


def subgraph_with_edges(graph: Graph, keep: np.ndarray) -> Graph:
    """Return a copy of `graph` keeping only the edges flagged in `keep`.

    Degree features are recomputed, because an observer who cannot see an
    edge also cannot see the degree it contributes. Leaving the original
    degrees in place would leak the hidden structure through the node
    features - the model would be told how many dependencies a component has
    while being denied the edges themselves.
    """
    keep = np.asarray(keep, dtype=bool)
    src, dst = graph.src[keep], graph.dst[keep]
    edge_features = graph.edge_features[keep]
    n = graph.num_nodes

    in_deg = np.zeros(n)
    out_deg = np.zeros(n)
    for e in range(src.shape[0]):
        in_deg[int(src[e])] += 1.0   # src is the dependency: things depend on it
        out_deg[int(dst[e])] += 1.0  # dst is the dependent

    denom = float(np.log1p(max(n - 1, 1)))
    node_features = graph.node_features.copy()
    node_features[:, _IN_DEG_COL] = (np.log1p(in_deg) / denom).astype(np.float32)
    node_features[:, _OUT_DEG_COL] = (np.log1p(out_deg) / denom).astype(np.float32)

    num_edges = src.shape[0]
    bi_src = np.concatenate([src, dst])
    bi_dst = np.concatenate([dst, src])
    flags = np.concatenate(
        [
            np.full((num_edges, 1), DIRECTION_REVERSE, dtype=np.float32),
            np.full((num_edges, 1), DIRECTION_FORWARD, dtype=np.float32),
        ]
    )
    bi_edge_features = np.concatenate(
        [np.concatenate([edge_features, edge_features], axis=0), flags], axis=1
    )

    return Graph(
        node_ids=graph.node_ids,
        node_features=node_features,
        src=src,
        dst=dst,
        edge_features=edge_features,
        names=list(graph.names),
        bi_src=bi_src,
        bi_dst=bi_dst,
        bi_edge_features=bi_edge_features,
    )


def _edge_betweenness_order(graph: Graph) -> np.ndarray:
    """Edge indices sorted by descending DIRECTED edge betweenness.

    Directedness is load-bearing and was got wrong first time round. Failure
    propagation follows directed paths, so an edge can sit on many undirected
    shortest paths while carrying no directed ones. Measured over 12 graphs at
    25% removal, targeting by undirected betweenness destroyed 37 reachable
    components against random removal's 38 - indistinguishable, i.e. not a
    targeted regime at all. Directed betweenness destroyed 61.

    Using the undirected projection would have meant the "catastrophic" arm of
    the experiment was quietly testing the benign case.
    """
    import networkx as nx

    g = nx.DiGraph()
    g.add_nodes_from(range(graph.num_nodes))
    # Several dependency rows can map onto the same ordered pair; keep a list
    # so every one of them inherits that pair's betweenness.
    pair_to_edges: dict[tuple[int, int], list[int]] = {}
    for e in range(graph.num_edges):
        key = (int(graph.src[e]), int(graph.dst[e]))
        pair_to_edges.setdefault(key, []).append(e)
        g.add_edge(*key)

    if g.number_of_edges() == 0:
        return np.arange(graph.num_edges)

    scores = nx.edge_betweenness_centrality(g)
    ranked = sorted(pair_to_edges, key=lambda k: -scores.get(k, 0.0))
    order: list[int] = []
    for key in ranked:
        order.extend(pair_to_edges[key])
    return np.asarray(order, dtype=int)


def hide_edges(
    graph: Graph,
    fraction: float,
    mode: str = "random",
    rng: np.random.Generator | None = None,
) -> tuple[Graph, np.ndarray]:
    """
    Hide a fraction of edges.

    Returns the observed graph and the boolean keep-mask, so callers can
    report exactly which dependencies were hidden.

    `mode`:
        "random"  uniform thinning (Chandrasekhar et al.'s benign case)
        "bridge"  remove the highest edge-betweenness edges first, i.e. the
                  shortcuts whose loss the theory says is catastrophic
    """
    if fraction <= 0 or graph.num_edges == 0:
        return graph, np.ones(graph.num_edges, dtype=bool)

    rng = rng or np.random.default_rng(0)
    n_remove = int(round(fraction * graph.num_edges))
    n_remove = min(n_remove, graph.num_edges)
    keep = np.ones(graph.num_edges, dtype=bool)

    if mode == "random":
        keep[rng.choice(graph.num_edges, size=n_remove, replace=False)] = False
    elif mode == "bridge":
        keep[_edge_betweenness_order(graph)[:n_remove]] = False
    else:
        raise ValueError(f"unknown removal mode {mode!r}; use 'random' or 'bridge'")

    return subgraph_with_edges(graph, keep), keep


def latent_candidate_pairs(graph: Graph, top_k: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Candidate (src, dst) pairs for latent edges: ordered non-adjacent pairs.

    Returns every ordered pair that is NOT already an observed propagation
    edge and is not a self-loop. SCP-Latent scores these from node features
    and keeps the best `top_k` per destination; the enumeration is done here
    so the model never has to know how the graph was built.

    Enumerating all N(N-1) pairs is quadratic. These graphs are tens of nodes,
    so that is cheap; on a large estate this would need blocking by component
    type or a nearest-neighbour index, which is noted as a limitation rather
    than solved here.
    """
    n = graph.num_nodes
    observed = {(int(graph.src[e]), int(graph.dst[e])) for e in range(graph.num_edges)}
    srcs, dsts = [], []
    for s in range(n):
        for t in range(n):
            if s == t or (s, t) in observed:
                continue
            srcs.append(s)
            dsts.append(t)
    if not srcs:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    return np.asarray(srcs, dtype=np.int64), np.asarray(dsts, dtype=np.int64)
