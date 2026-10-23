"""
Topology generator calibrated to a REAL production microservice graph.

WHY THIS EXISTS
---------------
Every result in docs/ml/ rests on graphs from ml/synth.py, whose realism was
never checked against a real system. ChronoGraph (Lutu et al. 2025,
arXiv:2509.04449, Apache-2.0) publishes the dependency graph of Bitdefender's
production microservices: 708 services, 1529 directed edges. Comparing it with
what ml/synth.py produces:

    property            REAL (ChronoGraph)      ml/synth.py
    edges / node                      2.16             1.32
    in-degree median                     1              1.2
    in-degree MAX                       73                4
    in-degree Gini                   0.781            0.536
    out-degree MAX                     103                5
    acyclic                             no      95% of graphs
    zero-blast-radius nodes            43%              42%
    max depth                            3                4

The generator gets two things right - the share of components nothing depends
on (43% vs 42%) and the shallow depth - and two things badly wrong: real
dependency graphs are **hub-dominated** (one service with in-degree 73 where
the synthetic maximum is 4) and **not acyclic** (22 short cycles; InfraTrace's
own triggers forbid cycles, so its schema cannot represent what the real system
does).

WHY THAT MATTERS FOR THE CONCLUSIONS ALREADY DRAWN
--------------------------------------------------
docs/ml/experiments.md reports that logistic regression on hand-engineered path
features ties SCP-GNN on ranking, and reads that as "message passing is not
what produces good ranking here". That conclusion was drawn entirely on
degree-homogeneous graphs.

Fountoulakis et al. (2023, arXiv:2310.07667) characterise GNN performance
against random-graph structure and report, among other results, that
**heavy-tailed degree distributions enhance GNN performance** and that GCNs
provably cannot beat linear models under some conditions. If that holds here,
the earlier tie is an artefact of an unrealistically homogeneous substrate, and
the gap should open up as degree concentration approaches the real value.

That is the falsifiable hypothesis this module exists to test. Nothing about
the mechanism is claimed as novel - the topology dependence is established
theory. What is being measured is whether InfraTrace's conclusion survives on a
realistic substrate.

THE ONE THING THAT CHANGES
--------------------------
Only wiring. Component creation, the node/edge feature encoding, and the
ground-truth cascade process are imported unchanged from ml/synth.py, so any
difference in model ranking is attributable to topology and nothing else. If
this module also altered the failure process, the experiment would confound the
two and prove nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .graph import Cascade, Graph, from_edges
from .synth import (
    DATASTORE_TYPES,
    TIER_CORE_SERVICE,
    TIER_DATASTORE,
    TIER_EDGE_SERVICE,
    TIER_EXTERNAL,
    TIER_GATEWAY,
    SynthConfig,
    _pick_dependency_type,
    _rand_int,
    simulate_cascade,
)

# Measured on ChronoGraph's edges.csv; see the module docstring.
REAL_IN_DEGREE_GINI = 0.781
REAL_OUT_DEGREE_GINI = 0.580
REAL_EDGES_PER_NODE = 2.16
REAL_ZERO_BLAST_FRACTION = 0.43
REAL_MAX_DEPTH = 3


@dataclass
class RealisticConfig:
    """Topology settings. The failure process is SynthConfig, untouched."""

    # Larger than ml/synth.py's graphs: hub structure cannot exist in 17 nodes,
    # and testing a degree-concentration hypothesis on graphs too small to have
    # a tail would be meaningless.
    num_nodes: tuple[int, int] = (40, 90)
    edges_per_node: float = REAL_EDGES_PER_NODE

    # Preferential attachment strength. A new dependency picks its target with
    # probability proportional to (in_degree + alpha). Small alpha means "rich
    # get richer", producing hubs and a high Gini; large alpha washes that out
    # towards uniform choice. This is the single knob the experiment sweeps.
    attachment_alpha: float = 0.6

    # Share of edges allowed to run against the tier ordering, which is what
    # creates cycles. The real graph has 22; a strict DAG generator cannot
    # produce any.
    back_edge_prob: float = 0.04

    tier_mix: tuple[float, ...] = (0.03, 0.17, 0.35, 0.33, 0.12)
    criticality_p: tuple[float, ...] = field(
        default_factory=lambda: (0.08, 0.30, 0.40, 0.22)
    )


def gini(x: np.ndarray) -> float:
    """Degree inequality. 0 = every node identical, 1 = all edges on one node."""
    x = np.sort(np.asarray(x, dtype=float))
    n = x.size
    if n == 0 or x.sum() == 0:
        return 0.0
    return float((2 * np.arange(1, n + 1) - n - 1).dot(x) / (n * x.sum()))


def _tier_of(i: int, bounds: list[int]) -> int:
    for tier, hi in enumerate(bounds):
        if i < hi:
            return tier
    return len(bounds) - 1


def generate_realistic_graph(
    rng: np.random.Generator,
    cfg: RealisticConfig,
    synth_cfg: SynthConfig,
) -> tuple[Graph, dict]:
    """Build one estate with hub-dominated, optionally cyclic wiring."""
    n = _rand_int(rng, cfg.num_nodes)
    mix = np.asarray(cfg.tier_mix, dtype=float)
    mix = mix / mix.sum()
    counts = np.maximum((mix * n).astype(int), 1)
    bounds = list(np.cumsum(counts))
    total = int(bounds[-1])

    type_choices = {
        TIER_GATEWAY: ["Gateway"],
        TIER_EDGE_SERVICE: ["Service"],
        TIER_CORE_SERVICE: ["Service"],
        TIER_DATASTORE: list(DATASTORE_TYPES),
        TIER_EXTERNAL: ["ExternalAPI"],
    }

    components: list[dict] = []
    tiers: list[int] = []
    for i in range(total):
        tier = _tier_of(i, bounds)
        ctype = str(rng.choice(type_choices[tier]))
        components.append(
            {
                "component_id": i + 1,
                "component_name": f"{ctype}-{i + 1}",
                "component_type": ctype,
                "criticality": str(
                    rng.choice(["Low", "Medium", "High", "Critical"], p=cfg.criticality_p)
                ),
                "is_active": 1,
            }
        )
        tiers.append(tier)

    tiers_arr = np.asarray(tiers)
    type_of = [c["component_type"] for c in components]

    # --- wiring by preferential attachment -------------------------------
    # in_count[v] is how many components already depend on v. Targets are drawn
    # proportional to (in_count + alpha), which is what manufactures hubs.
    in_count = np.zeros(total, dtype=np.float64)
    seen: set[tuple[int, int]] = set()
    dependencies: list[dict] = []
    target_edges = int(round(cfg.edges_per_node * total))

    attempts = 0
    while len(dependencies) < target_edges and attempts < target_edges * 40:
        attempts += 1
        u = int(rng.integers(0, total))  # the dependent
        below = np.nonzero(tiers_arr > tiers_arr[u])[0]
        if rng.random() < cfg.back_edge_prob or below.size == 0:
            # A back-edge or same-tier edge: this is what introduces cycles.
            candidates = np.nonzero(tiers_arr <= tiers_arr[u])[0]
            candidates = candidates[candidates != u]
            if candidates.size == 0:
                continue
        else:
            candidates = below

        w = in_count[candidates] + cfg.attachment_alpha
        v = int(rng.choice(candidates, p=w / w.sum()))
        if u == v or (u, v) in seen:
            continue
        seen.add((u, v))
        in_count[v] += 1.0
        dep_type, critical = _pick_dependency_type(rng, type_of[v], type_of[u])
        dependencies.append(
            {
                "component_id": components[u]["component_id"],
                "depends_on_id": components[v]["component_id"],
                "dependency_type": dep_type,
                "is_critical": critical,
            }
        )

    graph = from_edges(components, dependencies)

    # --- ground-truth failure parameters, identical to ml/synth.py --------
    num_nodes = graph.num_nodes
    mechanism = np.zeros(num_nodes, dtype=np.int8)
    roll = rng.random(num_nodes)
    mechanism[roll < synth_cfg.quorum_fraction] = 1
    mechanism[
        (roll >= synth_cfg.quorum_fraction)
        & (roll < synth_cfg.quorum_fraction + synth_cfg.resilient_fraction)
    ] = 2

    in_degree = np.zeros(num_nodes, dtype=int)
    for e in range(graph.num_edges):
        in_degree[int(graph.dst[e])] += 1

    quorum_k = np.ones(num_nodes, dtype=int)
    for i in range(num_nodes):
        if mechanism[i] == 1 and in_degree[i] >= 2:
            quorum_k[i] = int(
                rng.integers(2, min(synth_cfg.quorum_k_cap, in_degree[i]) + 1)
            )
        elif mechanism[i] == 1:
            mechanism[i] = 0

    damping = np.zeros(num_nodes, dtype=np.float64)
    lo, hi = synth_cfg.resilience_damping
    damping[mechanism == 2] = rng.uniform(lo, hi, size=int((mechanism == 2).sum()))

    jitter = 1.0 + rng.uniform(-synth_cfg.jitter, synth_cfg.jitter)
    edge_p = np.zeros(graph.num_edges, dtype=np.float64)
    edge_p_forward = np.zeros(graph.num_edges, dtype=np.float64)
    for e, d in enumerate(dependencies[: graph.num_edges]):
        base = synth_cfg.base_transmission.get(
            (d["dependency_type"], int(d["is_critical"])), 0.3
        )
        edge_p[e] = float(np.clip(base * jitter, 0.01, 0.99))
        fwd = synth_cfg.base_forward_transmission.get(
            (d["dependency_type"], int(d["is_critical"])), 0.15
        )
        edge_p_forward[e] = float(np.clip(fwd * jitter, 0.01, 0.95))

    truth = {
        "mechanism": mechanism,
        "quorum_k": quorum_k,
        "damping": damping,
        "edge_p": edge_p,
        "edge_p_forward": edge_p_forward,
        "in_degree": in_degree,
    }
    return graph, truth


def generate_realistic_dataset(
    seed: int,
    num_graphs: int,
    cascades_per_graph: int,
    cfg: RealisticConfig | None = None,
    synth_cfg: SynthConfig | None = None,
) -> list[tuple[Graph, list[Cascade]]]:
    """Datasets in the same shape as ml/synth.generate_dataset.

    Roots are sampled proportional to reachable-set size for the same reasons
    given there: a root that reaches nothing produces a scenario with nothing
    to predict. On a hub-dominated graph this matters more, because 43% of real
    components have no dependents at all.
    """
    cfg = cfg or RealisticConfig()
    synth_cfg = synth_cfg or SynthConfig()
    rng = np.random.default_rng(seed)
    out: list[tuple[Graph, list[Cascade]]] = []

    for _ in range(num_graphs):
        graph, truth = generate_realistic_graph(rng, cfg, synth_cfg)
        if graph.num_edges == 0:
            continue
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


def topology_report(dataset: list[tuple[Graph, list[Cascade]]]) -> dict:
    """Summarise a dataset's topology, for comparison with the real graph."""
    ins, outs, zero, depth, epn, br = [], [], [], [], [], []
    for graph, _cascades in dataset:
        n = graph.num_nodes
        in_d = np.zeros(n)
        out_d = np.zeros(n)
        for e in range(graph.num_edges):
            in_d[int(graph.dst[e])] += 1
            out_d[int(graph.src[e])] += 1
        ins.append(gini(in_d))
        outs.append(gini(out_d))
        epn.append(graph.num_edges / max(n, 1))
        radii = np.array([graph.reachable_from(i).sum() for i in range(n)])
        zero.append(float((radii == 0).mean()))
        br.append(float(radii.mean()))
        root = int(np.argmax(radii))
        hops = graph.hop_distance_from(root)
        depth.append(int(hops.max()))
    return {
        "in_degree_gini": float(np.mean(ins)),
        "out_degree_gini": float(np.mean(outs)),
        "edges_per_node": float(np.mean(epn)),
        "zero_blast_fraction": float(np.mean(zero)),
        "mean_blast_radius": float(np.mean(br)),
        "max_depth": float(np.mean(depth)),
    }
