"""
Sample held-out topologies from a REAL production dependency graph.

WHAT THIS IS FOR
----------------
Every conclusion in docs/ml/ was drawn on graphs from ml/synth.py. Measured
against ChronoGraph's real 708-service Bitdefender graph, that generator gets
the shallow depth and the share of zero-blast components right, and the degree
concentration badly wrong: real in-degree Gini 0.781 with a maximum of 73,
against roughly 0.5 and a maximum of 4.

That matters because Fountoulakis et al. (2023, arXiv:2310.07667) show
GNN advantage over linear models depends on exactly that property -
heavy-tailed degree distributions enhance GNN performance, and under some
conditions GCNs provably cannot beat linear models. docs/ml/experiments.md
reports logistic regression tying SCP-GNN on ranking; if that tie is an
artefact of a degree-homogeneous substrate, it should not survive here.

Rather than tuning a generator to imitate the real statistics, this module
uses the real graph directly: topologies are disjoint connected subgraphs of
it.

WHAT IS REAL HERE AND WHAT IS NOT
---------------------------------
REAL:      the dependency structure - which services depend on which.
SYNTHETIC: component types, criticality, and the failure process itself.
           ChronoGraph publishes no component attributes and no propagation
           labels, so cascades are simulated by ml/synth.simulate_cascade,
           unchanged.

So results here are a SYNTHETIC PROCESS ON REAL STRUCTURE. They are not
real-world validation of failure propagation, and must not be reported as
such. The only real propagation evidence in this project remains the 22
InfraTrace incidents. What this isolates is the effect of topology, because
everything except structure is held identical to the existing experiments.

LEAKAGE
-------
Sampled subgraphs are NODE-DISJOINT. Overlapping subgraphs would put the same
services - and the same local wiring - in train and test, so a model could
memorise a hub it will be scored on. `sample_subgraphs` partitions the node
set and never reuses a node.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .graph import Cascade, Graph, from_edges
from .synth import SynthConfig, simulate_cascade

DEFAULT_EDGES_CSV = Path(__file__).resolve().parent / "data" / "chronograph" / "edges.csv"

# Marginal distribution of criticality in InfraTrace's real component table.
# Sampled INDEPENDENTLY of degree on purpose: tying criticality to the number
# of dependents would manufacture a feature that predicts blast radius, and on
# the real InfraTrace data criticality does not even track impact severity
# (docs/ml/parsimony-study.md).
CRITICALITY_MARGINAL = ("Low", "Medium", "High", "Critical")
CRITICALITY_P = (0.08, 0.30, 0.40, 0.22)


@dataclass
class RealTopoConfig:
    nodes_per_subgraph: tuple[int, int] = (35, 80)
    min_edges: int = 20
    # Structural role -> component type. A component nothing depends on sits at
    # the top of the stack (gateway-like); one that depends on nothing sits at
    # the bottom (datastore-like). This is inferred from the real wiring rather
    # than invented, which is the most defensible assignment available given
    # ChronoGraph publishes no component attributes.
    datastore_types: tuple[str, ...] = ("Database", "Cache", "Queue", "Storage")
    criticality_p: tuple[float, ...] = field(default_factory=lambda: CRITICALITY_P)


def load_real_edges(path: Path | str = DEFAULT_EDGES_CSV) -> tuple[list[str], list[str]]:
    """Read the ChronoGraph edge list. `source depends on target`."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Fetch it with:\n"
            "  curl -sL -o ml/data/chronograph/edges.csv \\\n"
            "    https://raw.githubusercontent.com/bit-ml/ChronoGraph/main/dataset/edges.csv\n"
            "See ml/data/chronograph/SOURCE.md."
        )
    src: list[str] = []
    dst: list[str] = []
    with p.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            src.append(row["source"])
            dst.append(row["target"])
    return src, dst


def _adjacency(src_i: np.ndarray, dst_i: np.ndarray, n: int) -> list[set[int]]:
    """Undirected adjacency, used only for growing connected subgraphs."""
    adj: list[set[int]] = [set() for _ in range(n)]
    for a, b in zip(src_i.tolist(), dst_i.tolist()):
        adj[a].add(b)
        adj[b].add(a)
    return adj


def split_node_pools(
    seed: int = 0,
    fractions: tuple[float, float, float] = (0.6, 0.15, 0.25),
    path: Path | str = DEFAULT_EDGES_CSV,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Partition the real graph's services into disjoint train/val/test pools.

    THIS is what prevents leakage, and it is done once at the node level rather
    than per subgraph. Carving strictly disjoint subgraphs instead yields only
    ~14 topologies from 708 services - too few to split - and leaves half the
    graph unused because the leftovers fragment. Partitioning the node set
    first lets each pool be sampled repeatedly (subgraphs within a pool may
    overlap, which is ordinary augmentation) while guaranteeing that no service
    appearing in training can ever appear in test.

    The split is by BREADTH-FIRST ORDER, not a random permutation. A random
    25% of a graph this sparse is shattered - growth confined to it cannot
    reach 20 connected services, and the validation pool yielded no usable
    topology at all. BFS order keeps each pool contiguous, so every pool can
    still produce connected subgraphs. The cost is that pools are adjacent
    regions of one system rather than independent samples, which is stated as
    a limitation in docs/ml/topology-study.md rather than hidden.
    """
    src, dst = load_real_edges(path)
    nodes = sorted(set(src) | set(dst))
    index = {name: i for i, name in enumerate(nodes)}
    n = len(nodes)
    src_i = np.array([index[a] for a in src], dtype=np.int64)
    dst_i = np.array([index[b] for b in dst], dtype=np.int64)
    adj = _adjacency(src_i, dst_i, n)

    rng = np.random.default_rng(seed)
    order: list[int] = []
    seen = np.zeros(n, dtype=bool)
    for start in rng.permutation(n):
        start = int(start)
        if seen[start]:
            continue
        seen[start] = True
        queue = [start]
        while queue:
            node = queue.pop(0)
            order.append(node)
            nbrs = sorted(adj[node])
            rng.shuffle(nbrs)
            for nb in nbrs:
                if not seen[nb]:
                    seen[nb] = True
                    queue.append(nb)
    arr = np.asarray(order, dtype=np.int64)
    n_train = int(fractions[0] * n)
    n_val = int(fractions[1] * n)
    return arr[:n_train], arr[n_train : n_train + n_val], arr[n_train + n_val :]


def sample_subgraphs(
    seed: int,
    num_subgraphs: int,
    cfg: RealTopoConfig | None = None,
    path: Path | str = DEFAULT_EDGES_CSV,
    node_pool: np.ndarray | None = None,
    disjoint: bool = True,
) -> list[tuple[list[dict], list[dict]]]:
    """Carve connected subgraphs out of the real graph.

    `node_pool` restricts sampling to one split's services. `disjoint=False`
    allows subgraphs within a pool to overlap, which is how enough topologies
    are obtained; cross-split disjointness is guaranteed by the pool itself.

    Returns (components, dependencies) record pairs in the shape
    ml/graph.from_edges expects, so the rest of the pipeline is unchanged.
    """
    cfg = cfg or RealTopoConfig()
    rng = np.random.default_rng(seed)

    src, dst = load_real_edges(path)
    nodes = sorted(set(src) | set(dst))
    index = {name: i for i, name in enumerate(nodes)}
    src_i = np.array([index[a] for a in src], dtype=np.int64)
    dst_i = np.array([index[b] for b in dst], dtype=np.int64)
    n = len(nodes)
    adj = _adjacency(src_i, dst_i, n)

    allowed = np.zeros(n, dtype=bool)
    allowed[np.arange(n) if node_pool is None else node_pool] = True
    # Growth may only enter the pool, so a subgraph never straddles a split.
    adj = [{x for x in nb if allowed[x]} for nb in adj]

    used = np.zeros(n, dtype=bool)
    used[~allowed] = True
    out: list[tuple[list[dict], list[dict]]] = []

    pool_nodes = np.nonzero(allowed)[0]
    order = rng.permutation(pool_nodes)
    cursor = 0
    attempts = 0
    while len(out) < num_subgraphs and attempts < num_subgraphs * 30:
        attempts += 1
        if disjoint:
            while cursor < order.size and used[int(order[cursor])]:
                cursor += 1
            if cursor >= order.size:
                break
            start = int(order[cursor])
            cursor += 1
        else:
            # Overlap permitted within a pool: reset the visited marks so each
            # subgraph is grown independently from a fresh random seed node.
            used = ~allowed.copy()
            start = int(rng.choice(pool_nodes))

        target = int(rng.integers(cfg.nodes_per_subgraph[0], cfg.nodes_per_subgraph[1] + 1))
        chosen: list[int] = [start]
        used[start] = True
        frontier = [start]
        while frontier and len(chosen) < target:
            node = frontier.pop(0)
            neighbours = [x for x in adj[node] if not used[x]]
            rng.shuffle(neighbours)
            for nb in neighbours:
                if len(chosen) >= target:
                    break
                used[nb] = True
                chosen.append(nb)
                frontier.append(nb)

        keep = set(chosen)
        mask = np.array(
            [(int(a) in keep and int(b) in keep) for a, b in zip(src_i, dst_i)],
            dtype=bool,
        )
        if int(mask.sum()) < cfg.min_edges:
            continue

        local = {g: k for k, g in enumerate(sorted(keep))}
        sub_src = [local[int(a)] for a in src_i[mask]]
        sub_dst = [local[int(b)] for b in dst_i[mask]]

        # Structural roles, read off the real wiring of this subgraph.
        m = len(local)
        dependents = np.zeros(m)   # how many depend on me
        depends_on = np.zeros(m)   # how many I depend on
        for a, b in zip(sub_src, sub_dst):
            depends_on[a] += 1     # a depends on b
            dependents[b] += 1

        components: list[dict] = []
        for i in range(m):
            if depends_on[i] == 0 and dependents[i] > 0:
                ctype = str(rng.choice(cfg.datastore_types))
            elif dependents[i] == 0:
                ctype = "Gateway"
            else:
                ctype = "Service"
            components.append(
                {
                    "component_id": i + 1,
                    "component_name": f"{ctype}-{i + 1}",
                    "component_type": ctype,
                    "criticality": str(
                        rng.choice(CRITICALITY_MARGINAL, p=cfg.criticality_p)
                    ),
                    "is_active": 1,
                }
            )

        from .synth import _pick_dependency_type

        type_of = [c["component_type"] for c in components]
        dependencies: list[dict] = []
        for a, b in zip(sub_src, sub_dst):
            dep_type, critical = _pick_dependency_type(rng, type_of[b], type_of[a])
            dependencies.append(
                {
                    "component_id": a + 1,       # the dependent
                    "depends_on_id": b + 1,      # the dependency
                    "dependency_type": dep_type,
                    "is_critical": critical,
                }
            )
        out.append((components, dependencies))

    return out


def _truth_for(graph: Graph, dependencies: list[dict], rng, synth_cfg: SynthConfig) -> dict:
    """Ground-truth failure parameters, identical to ml/synth.generate_graph."""
    n = graph.num_nodes
    mechanism = np.zeros(n, dtype=np.int8)
    roll = rng.random(n)
    mechanism[roll < synth_cfg.quorum_fraction] = 1
    mechanism[
        (roll >= synth_cfg.quorum_fraction)
        & (roll < synth_cfg.quorum_fraction + synth_cfg.resilient_fraction)
    ] = 2

    in_degree = np.zeros(n, dtype=int)
    for e in range(graph.num_edges):
        in_degree[int(graph.dst[e])] += 1

    quorum_k = np.ones(n, dtype=int)
    for i in range(n):
        if mechanism[i] == 1 and in_degree[i] >= 2:
            quorum_k[i] = int(rng.integers(2, min(synth_cfg.quorum_k_cap, in_degree[i]) + 1))
        elif mechanism[i] == 1:
            mechanism[i] = 0

    damping = np.zeros(n, dtype=np.float64)
    lo, hi = synth_cfg.resilience_damping
    damping[mechanism == 2] = rng.uniform(lo, hi, size=int((mechanism == 2).sum()))

    jitter = 1.0 + rng.uniform(-synth_cfg.jitter, synth_cfg.jitter)
    edge_p = np.zeros(graph.num_edges, dtype=np.float64)
    edge_p_fwd = np.zeros(graph.num_edges, dtype=np.float64)
    for e, d in enumerate(dependencies[: graph.num_edges]):
        base = synth_cfg.base_transmission.get(
            (d["dependency_type"], int(d["is_critical"])), 0.3
        )
        edge_p[e] = float(np.clip(base * jitter, 0.01, 0.99))
        fwd = synth_cfg.base_forward_transmission.get(
            (d["dependency_type"], int(d["is_critical"])), 0.15
        )
        edge_p_fwd[e] = float(np.clip(fwd * jitter, 0.01, 0.95))

    return {
        "mechanism": mechanism,
        "quorum_k": quorum_k,
        "damping": damping,
        "edge_p": edge_p,
        "edge_p_forward": edge_p_fwd,
        "in_degree": in_degree,
    }


def generate_real_topology_dataset(
    seed: int,
    num_graphs: int,
    cascades_per_graph: int,
    cfg: RealTopoConfig | None = None,
    synth_cfg: SynthConfig | None = None,
    path: Path | str = DEFAULT_EDGES_CSV,
    node_pool: np.ndarray | None = None,
    disjoint: bool = True,
) -> list[tuple[Graph, list[Cascade]]]:
    """Datasets on real structure, in the same shape as ml/synth.generate_dataset.

    The cascade process is ml/synth.simulate_cascade, unchanged, so a model
    comparison run on these graphs differs from the existing experiments in
    topology and nothing else.
    """
    cfg = cfg or RealTopoConfig()
    synth_cfg = synth_cfg or SynthConfig()
    rng = np.random.default_rng(seed)

    out: list[tuple[Graph, list[Cascade]]] = []
    for components, dependencies in sample_subgraphs(
        seed, num_graphs, cfg, path, node_pool=node_pool, disjoint=disjoint
    ):
        graph = from_edges(components, dependencies)
        if graph.num_edges == 0:
            continue
        truth = _truth_for(graph, dependencies, rng, synth_cfg)

        reach = np.array(
            [graph.reachable_from(i).sum() for i in range(graph.num_nodes)],
            dtype=np.float64,
        )
        if reach.sum() == 0:
            continue
        weights = reach / reach.sum()
        cascades = [
            simulate_cascade(
                rng, graph, truth, int(rng.choice(graph.num_nodes, p=weights)), synth_cfg
            )
            for _ in range(cascades_per_graph)
        ]
        out.append((graph, cascades))
    return out
