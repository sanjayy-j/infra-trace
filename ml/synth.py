"""
Synthetic dependency graphs and failure cascades.

WHY THIS EXISTS
---------------
The real ShopSphere graph has 19 components, 29 edges and 22 recorded
incidents (one root cause each). That is enough to *evaluate* a model on a
handful of real scenarios, and nowhere near enough to *train* one. Training
therefore happens on synthetic graphs, and the real incidents are held back
as a small real-world check. This mirrors RiskNet (Rusek et al., 2022,
arXiv:2201.12263), which trains only on generated Barabasi-Albert graphs and
transfers to real network topologies.

THE CIRCULARITY TRAP, AND HOW THIS GENERATOR AVOIDS IT
------------------------------------------------------
The model proposed in ml/models/scp.py composes per-edge transmission
probabilities with noisy-OR, which is the marginal form of an Independent
Cascade (IC) process. If this generator simulated a pure IC process, the
model would be fitting data drawn from its own hypothesis class and would
win by construction. Any reported improvement would be an artefact.

So the ground-truth process here is deliberately NOT pure IC. Every node is
assigned one of three failure mechanisms:

  * OR nodes       fail if ANY incoming dependency transmits failure.
                   This is the IC-representable case.
  * QUORUM nodes   fail only when at least k of their dependencies have
                   failed. A threshold rule. Noisy-OR CANNOT express this:
                   it is not a function of each edge independently.
  * RESILIENT nodes have retries / circuit breakers that damp every incoming
                   transmission probability by a hidden per-node factor.

Labels then pass through observation noise, because real incident records
are written by humans under time pressure and miss things.

The proposed model is therefore *misspecified* with respect to the data it
is trained on, which is the honest setting: it has to approximate a process
it cannot represent exactly, and the flexible GNN baselines are free to do
better. Section "quorum_fraction" in the config controls how much of the
data lies outside the model's hypothesis class; the experiment runner sweeps
it to show where the proposed model's advantage breaks down.

ASSUMPTIONS, STATED EXPLICITLY
------------------------------
Every number below is an assumption, not a measurement. The base
transmission rates are *informed* by rates measured on the real seed data
(ml/experiments: Synchronous+critical ~86%, Synchronous ~50%, Data ~50%,
Asynchronous ~40%) but are jittered per graph so a model cannot simply
memorise four constants and must actually read the features.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .graph import Cascade, Graph, from_edges

# ---------------------------------------------------------------------
# Architectural tiers. A microservice estate is not a random graph: traffic
# enters at a gateway, fans out through services, and terminates at
# datastores and third-party APIs. Edges run from a tier towards the tiers
# below it. This structure is what gives the graph its characteristic
# shallow-and-wide blast radii.
# ---------------------------------------------------------------------
TIER_GATEWAY = 0
TIER_EDGE_SERVICE = 1
TIER_CORE_SERVICE = 2
TIER_DATASTORE = 3
TIER_EXTERNAL = 4

DATASTORE_TYPES = ("Database", "Cache", "Queue", "Storage")


@dataclass
class SynthConfig:
    """Generator settings. Every field is an assumption; see module docstring."""

    # --- topology ---
    num_gateways: tuple[int, int] = (1, 2)
    num_edge_services: tuple[int, int] = (2, 5)
    num_core_services: tuple[int, int] = (3, 8)
    num_datastores: tuple[int, int] = (3, 7)
    num_external: tuple[int, int] = (1, 3)
    # Probability a service also depends on a peer in the same tier. Keeps
    # the graph from being a clean DAG of layers and creates longer chains.
    peer_edge_prob: float = 0.25
    # Probability a service skips a tier (gateway depending straight on a
    # datastore). Real systems have these and they shorten blast paths.
    skip_edge_prob: float = 0.12

    # --- ground-truth failure mechanism ---
    # Fractions must sum to <= 1; the remainder are OR nodes.
    quorum_fraction: float = 0.25
    resilient_fraction: float = 0.25
    # k for a quorum node is drawn uniformly from [2, min(cap, in_degree)].
    quorum_k_cap: int = 3
    # Resilient nodes multiply every incoming transmission probability by
    # (1 - damping), with damping drawn from this range.
    resilience_damping: tuple[float, float] = (0.35, 0.75)

    # --- base transmission probabilities by (dependency_type, is_critical) ---
    # Informed by the real seed data, jittered per graph (see `jitter`).
    base_transmission: dict[tuple[str, int], float] = field(
        default_factory=lambda: {
            ("Synchronous", 1): 0.88,
            ("Synchronous", 0): 0.52,
            ("Data", 1): 0.70,
            ("Data", 0): 0.48,
            ("Asynchronous", 1): 0.45,
            ("Asynchronous", 0): 0.18,
            ("Config", 1): 0.40,
            ("Config", 0): 0.15,
        }
    )
    # Per-graph multiplicative jitter on the table above, so that the mapping
    # from edge type to probability varies between estates. A model that
    # memorises constants rather than reading features will lose here.
    jitter: float = 0.15

    # --- FORWARD propagation: a component damaging what it depends on ---
    #
    # Measured on the 22 real incidents, forward edges carry failure 31.8% of
    # the time against 61.9% for reverse ones, so roughly half. Base rates
    # below are set from that ratio, with Data near zero (observed 0 of 3).
    #
    # The MECHANISM is deliberately different in kind from the reverse one.
    # Reverse propagation is a per-edge independent draw, which noisy-OR
    # represents exactly. Forward propagation here is RETRY AMPLIFICATION: the
    # probability that u damages a dependency v grows with how many of u's own
    # callers have ALREADY failed, because more stalled callers means more
    # retry pressure. That is a function of the live cascade state, not of each
    # edge independently, so no static per-edge probability composed by
    # noisy-OR can represent it. Without this, generating forward propagation
    # as plain independent-cascade draws would hand SCP-BiD data drawn from its
    # own hypothesis class and the experiment would be rigged in its favour.
    # DEFAULT OFF. Turning it on changes the ground-truth process, and so
    # every number produced by the original benchmark. Leaving it off keeps
    # all prior results in docs/ml/experiments.md reproducible exactly as
    # published; the directionality experiment sets it True explicitly.
    enable_forward_propagation: bool = False
    # Calibrated so that the synthetic forward/reverse 1-hop propagation ratio
    # matches the ratio measured on the 22 real incidents (31.8% / 61.9% =
    # 0.51). Rates were swept rather than assumed; see docs/ml/direction-study.md.
    base_forward_transmission: dict[tuple[str, int], float] = field(
        default_factory=lambda: {
            ("Synchronous", 1): 0.248,
            ("Synchronous", 0): 0.198,
            ("Asynchronous", 1): 0.217,
            ("Asynchronous", 0): 0.136,
            ("Config", 1): 0.074,
            ("Config", 0): 0.062,
            ("Data", 1): 0.037,
            ("Data", 0): 0.031,
        }
    )
    # How strongly failed callers amplify forward transmission. Non-zero is
    # load-bearing: it is what puts the forward mechanism outside noisy-OR's
    # hypothesis class.
    forward_load_gain: float = 0.8

    # --- observation noise on the recorded labels ---
    false_negative_rate: float = 0.05  # affected but not written down
    false_positive_rate: float = 0.02  # recorded but not actually caused

    max_rounds: int = 12


def _rand_int(rng: np.random.Generator, bounds: tuple[int, int]) -> int:
    lo, hi = bounds
    return int(rng.integers(lo, hi + 1))


def _pick_dependency_type(
    rng: np.random.Generator, src_type: str, dst_type: str
) -> tuple[str, int]:
    """Choose a dependency type consistent with the two endpoint types.

    A service talking to a queue is asynchronous; a service reading a
    database is synchronous or data. Sampling edge type independently of the
    endpoints would make edge features trivially redundant with node
    features and would flatter any model that uses them.
    """
    if src_type == "Queue":
        dep_type = "Asynchronous"
        critical = int(rng.random() < 0.25)
    elif src_type == "Cache":
        # Caches are usually optional: a cache miss degrades, rarely kills.
        dep_type = rng.choice(["Synchronous", "Data"], p=[0.7, 0.3])
        critical = int(rng.random() < 0.20)
    elif src_type == "Database":
        dep_type = rng.choice(["Synchronous", "Data"], p=[0.6, 0.4])
        critical = int(rng.random() < 0.75)
    elif src_type == "Storage":
        dep_type = rng.choice(["Data", "Asynchronous"], p=[0.7, 0.3])
        critical = int(rng.random() < 0.35)
    elif src_type == "ExternalAPI":
        dep_type = "Synchronous"
        critical = int(rng.random() < 0.55)
    else:  # Service or Gateway
        dep_type = rng.choice(
            ["Synchronous", "Asynchronous", "Config"], p=[0.72, 0.20, 0.08]
        )
        critical = int(rng.random() < 0.50)
    return str(dep_type), critical


def generate_graph(
    rng: np.random.Generator, cfg: SynthConfig
) -> tuple[Graph, dict]:
    """Build one synthetic estate.

    Returns the Graph plus a `truth` dict holding the hidden parameters of
    the ground-truth failure process. The truth dict is used only by
    `simulate_cascade`; no model ever sees it.
    """
    components: list[dict] = []
    tiers: list[int] = []
    cid = 1

    def add(n: int, tier: int, type_choices: list[str], crit_p: list[float]) -> None:
        nonlocal cid
        for _ in range(n):
            ctype = str(rng.choice(type_choices))
            components.append(
                {
                    "component_id": cid,
                    "component_name": f"{ctype}-{cid}",
                    "component_type": ctype,
                    "criticality": str(
                        rng.choice(["Low", "Medium", "High", "Critical"], p=crit_p)
                    ),
                    "is_active": 1,
                }
            )
            tiers.append(tier)
            cid += 1

    add(_rand_int(rng, cfg.num_gateways), TIER_GATEWAY, ["Gateway"], [0.0, 0.05, 0.25, 0.70])
    add(_rand_int(rng, cfg.num_edge_services), TIER_EDGE_SERVICE, ["Service"], [0.05, 0.25, 0.45, 0.25])
    add(_rand_int(rng, cfg.num_core_services), TIER_CORE_SERVICE, ["Service"], [0.05, 0.30, 0.40, 0.25])
    add(_rand_int(rng, cfg.num_datastores), TIER_DATASTORE, list(DATASTORE_TYPES), [0.05, 0.20, 0.40, 0.35])
    add(_rand_int(rng, cfg.num_external), TIER_EXTERNAL, ["ExternalAPI"], [0.10, 0.35, 0.40, 0.15])

    tiers_arr = np.asarray(tiers)
    by_tier = {t: np.nonzero(tiers_arr == t)[0] for t in range(5)}
    type_of = [c["component_type"] for c in components]

    # --- wire dependencies: u (higher tier) depends on v (lower tier) ---
    dependencies: list[dict] = []
    seen: set[tuple[int, int]] = set()

    def connect(u_idx: int, v_idx: int) -> None:
        if u_idx == v_idx:
            return
        key = (u_idx, v_idx)
        if key in seen:
            return
        seen.add(key)
        dep_type, critical = _pick_dependency_type(rng, type_of[v_idx], type_of[u_idx])
        dependencies.append(
            {
                "component_id": components[u_idx]["component_id"],
                "depends_on_id": components[v_idx]["component_id"],
                "dependency_type": dep_type,
                "is_critical": critical,
            }
        )

    for tier in (TIER_GATEWAY, TIER_EDGE_SERVICE, TIER_CORE_SERVICE):
        below = np.concatenate(
            [by_tier[t] for t in range(tier + 1, 5) if by_tier[t].size]
            or [np.array([], dtype=int)]
        )
        if below.size == 0:
            continue
        for u in by_tier[tier]:
            # Each component depends on 1-3 things in the tiers below it.
            k = int(rng.integers(1, 4))
            for v in rng.choice(below, size=min(k, below.size), replace=False):
                connect(int(u), int(v))
            # Occasional peer dependency within the same tier.
            peers = by_tier[tier]
            if peers.size > 1 and rng.random() < cfg.peer_edge_prob:
                v = int(rng.choice(peers))
                connect(int(u), v)
            # Occasional tier skip straight to a datastore.
            if by_tier[TIER_DATASTORE].size and rng.random() < cfg.skip_edge_prob:
                connect(int(u), int(rng.choice(by_tier[TIER_DATASTORE])))

    graph = from_edges(components, dependencies)

    # --- hidden ground-truth parameters ---
    n = graph.num_nodes
    mechanism = np.zeros(n, dtype=np.int8)  # 0 = OR, 1 = QUORUM, 2 = RESILIENT
    roll = rng.random(n)
    mechanism[roll < cfg.quorum_fraction] = 1
    mechanism[
        (roll >= cfg.quorum_fraction)
        & (roll < cfg.quorum_fraction + cfg.resilient_fraction)
    ] = 2

    in_degree = np.zeros(n, dtype=int)
    for e in range(graph.num_edges):
        in_degree[int(graph.dst[e])] += 1

    quorum_k = np.ones(n, dtype=int)
    for i in range(n):
        if mechanism[i] == 1 and in_degree[i] >= 2:
            quorum_k[i] = int(rng.integers(2, min(cfg.quorum_k_cap, in_degree[i]) + 1))
        else:
            # A quorum node with fewer than two dependencies degenerates to OR.
            mechanism[i] = 0 if mechanism[i] == 1 else mechanism[i]

    damping = np.zeros(n, dtype=np.float64)
    lo, hi = cfg.resilience_damping
    damping[mechanism == 2] = rng.uniform(lo, hi, size=int((mechanism == 2).sum()))

    # Per-edge true transmission probability.
    edge_p = np.zeros(graph.num_edges, dtype=np.float64)
    jitter = 1.0 + rng.uniform(-cfg.jitter, cfg.jitter)
    for e, d in enumerate(dependencies):
        base = cfg.base_transmission.get(
            (d["dependency_type"], int(d["is_critical"])), 0.3
        )
        edge_p[e] = float(np.clip(base * jitter, 0.01, 0.99))

    # Forward (dependent -> dependency) base probabilities, indexed the same
    # way as edge_p: entry e corresponds to dependency row e, traversed from
    # its `component_id` towards its `depends_on_id`.
    edge_p_forward = np.zeros(graph.num_edges, dtype=np.float64)
    for e, d in enumerate(dependencies):
        base = cfg.base_forward_transmission.get(
            (d["dependency_type"], int(d["is_critical"])), 0.15
        )
        edge_p_forward[e] = float(np.clip(base * jitter, 0.01, 0.95))

    truth = {
        "mechanism": mechanism,
        "quorum_k": quorum_k,
        "damping": damping,
        "edge_p": edge_p,
        "edge_p_forward": edge_p_forward,
        "in_degree": in_degree,
    }
    return graph, truth


def simulate_cascade(
    rng: np.random.Generator,
    graph: Graph,
    truth: dict,
    root_index: int,
    cfg: SynthConfig,
) -> Cascade:
    """Run the ground-truth failure process once from `root_index`.

    Two mechanisms run together, in opposite directions:

    REVERSE - a dependency fails and its dependents suffer. Mixed OR / QUORUM
    / RESILIENT, per-edge independent draws. This is the original process.

    FORWARD - a component damages something it depends on, by retry
    amplification. The probability scales with how many of the damaging
    component's OWN callers have already failed, so it is a function of the
    live cascade state rather than of each edge independently. noisy-OR
    cannot represent it, which is the point: see the note on
    `enable_forward_propagation` in SynthConfig.

    Rounds proceed until no new node fails or `max_rounds` is hit.
    """
    n = graph.num_nodes
    failed = np.zeros(n, dtype=bool)
    failed[root_index] = True

    # Incoming propagation edges per node.
    incoming: dict[int, list[int]] = {}
    for e in range(graph.num_edges):
        incoming.setdefault(int(graph.dst[e]), []).append(e)

    # Outgoing-forward edges per node: edges whose DEPENDENT is this node, i.e.
    # the things it depends on and could damage.
    forward_out: dict[int, list[int]] = {}
    for e in range(graph.num_edges):
        forward_out.setdefault(int(graph.dst[e]), []).append(e)

    mechanism = truth["mechanism"]
    quorum_k = truth["quorum_k"]
    damping = truth["damping"]
    edge_p = truth["edge_p"]
    edge_p_forward = truth.get("edge_p_forward")
    in_degree = truth.get("in_degree")

    # An edge gets one transmission draw for the whole cascade, which is the
    # standard "live-edge" view: a dependency either carries the failure or
    # it does not, and re-rolling it each round would let a failure sneak
    # through a dependency that already held.
    live = rng.random(graph.num_edges)
    do_forward = (
        cfg.enable_forward_propagation
        and edge_p_forward is not None
        and in_degree is not None
    )
    # Drawn only when forward propagation is enabled. Drawing it
    # unconditionally would consume random state and shift the whole stream,
    # silently changing every dataset generated with the mechanism OFF - and
    # with it every number already published in docs/ml/experiments.md.
    live_forward = rng.random(graph.num_edges) if do_forward else None

    for _ in range(cfg.max_rounds):
        newly: set[int] = set()

        # --- reverse: dependency fails, dependents suffer ---
        for v in range(n):
            if failed[v]:
                continue
            edges = incoming.get(v, ())
            if not edges:
                continue
            transmitted = 0
            for e in edges:
                if not failed[int(graph.src[e])]:
                    continue
                p = edge_p[e]
                if mechanism[v] == 2:
                    p *= 1.0 - damping[v]
                if live[e] < p:
                    transmitted += 1
            needed = quorum_k[v] if mechanism[v] == 1 else 1
            if transmitted >= needed:
                newly.add(v)

        # --- forward: failing component damages what it depends on ---
        if do_forward:
            for u in range(n):
                if not failed[u]:
                    continue
                # Retry pressure: how many of u's own callers have failed.
                # A service whose callers are all stalled retries hardest.
                callers = [int(graph.dst[e]) for e in range(graph.num_edges)
                           if int(graph.src[e]) == u]
                failed_callers = sum(1 for w in callers if failed[w])
                amplification = 1.0 + cfg.forward_load_gain * (
                    failed_callers / max(1, int(in_degree[u]) if in_degree[u] else 1)
                )
                for e in forward_out.get(u, ()):
                    target = int(graph.src[e])   # the thing u depends on
                    if failed[target]:
                        continue
                    p = float(np.clip(edge_p_forward[e] * amplification, 0.0, 0.95))
                    if mechanism[target] == 2:
                        p *= 1.0 - damping[target]
                    if live_forward[e] < p:
                        newly.add(target)

        if not newly:
            break
        failed[list(newly)] = True

    labels = failed.astype(np.float32)
    labels[root_index] = 1.0

    # --- observation noise ---
    affected = np.nonzero(labels > 0.5)[0]
    affected = affected[affected != root_index]
    if affected.size:
        drop = rng.random(affected.size) < cfg.false_negative_rate
        labels[affected[drop]] = 0.0
    unaffected = np.nonzero(labels < 0.5)[0]
    if unaffected.size:
        add = rng.random(unaffected.size) < cfg.false_positive_rate
        # Only a reachable component can plausibly be mis-recorded as
        # affected by this root cause; an unreachable one would be a
        # different incident entirely. With forward propagation enabled the
        # relevant notion of reachable is the bidirectional one.
        reach = (
            graph.reachable_bidirectional(root_index)
            if cfg.enable_forward_propagation
            else graph.reachable_from(root_index)
        )
        spurious = unaffected[add & reach[unaffected]]
        labels[spurious] = 1.0

    return Cascade(root_index=root_index, labels=labels)


def generate_dataset(
    seed: int,
    num_graphs: int,
    cascades_per_graph: int,
    cfg: SynthConfig | None = None,
) -> list[tuple[Graph, list[Cascade]]]:
    """Generate `num_graphs` estates, each with `cascades_per_graph` incidents.

    ROOT SAMPLING, AND THE SELECTION BIAS IT INTRODUCES
    ---------------------------------------------------
    Roots are sampled with probability proportional to the size of their
    reachable set, not uniformly. Two reasons, one practical and one about
    realism:

      * Practical: a root that can reach nothing produces a scenario with no
        component to predict. Those carry no signal, make PR-AUC undefined on
        a split that happens to collect several of them, and would pad the
        data with scenarios every model gets right for free.
      * Realism: incidents are recorded when something visible breaks. A leaf
        component with no dependents rarely generates a postmortem, so a
        corpus of real incidents is biased towards influential components in
        exactly this way.

    This is still a bias and it is worth being explicit that it makes the
    task HARDER for the proposed model rather than easier: it concentrates
    the data on large reachable sets, which is precisely where reachability
    over-predicts most and where a probabilistic model has the most room to
    be wrong. Scenarios whose reachable set is empty are skipped entirely.
    """
    cfg = cfg or SynthConfig()
    rng = np.random.default_rng(seed)
    dataset: list[tuple[Graph, list[Cascade]]] = []

    for _ in range(num_graphs):
        graph, truth = generate_graph(rng, cfg)
        if graph.num_edges == 0:
            continue

        reach_size = np.array(
            [graph.reachable_from(i).sum() for i in range(graph.num_nodes)],
            dtype=np.float64,
        )
        if reach_size.sum() == 0:
            continue
        weights = reach_size / reach_size.sum()

        cascades = [
            simulate_cascade(
                rng, graph, truth, int(rng.choice(graph.num_nodes, p=weights)), cfg
            )
            for _ in range(cascades_per_graph)
        ]
        dataset.append((graph, cascades))

    return dataset
