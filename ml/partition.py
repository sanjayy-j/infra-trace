"""
Hub-stratified partitioning of the real dependency graph.

This is the evaluation substrate for the corrected topology study specified in
docs/ml/topology-study.md. It replaces the previous
`realtopo.split_node_pools`, which was called with a hardcoded seed so that
every "seed" of the experiment shared one train/val/test split, and which put
all ten largest hubs in training.

Nothing here touches any model. SCP-GNN remains the default and is not
imported.

WHAT THIS MODULE GUARANTEES
---------------------------
1. Every service lands in exactly one pool. A hub is never duplicated; edges
   crossing a pool boundary are DROPPED and counted, never copied.
2. A hub keeps the dependents it claims, so hub structure survives the split
   as far as the graph allows. Retention is measured, not assumed.
3. Per-tier quotas are allocated by largest remainder, so they sum to the
   exact tier size for any tier size and any share vector.
4. Pool-size deviation is bounded; partitions outside tolerance are rejected
   on STRUCTURAL grounds only, before any model is trained, so rejection
   cannot bias a model comparison.

HUB PRESENCE IS NOT HUB PRESERVATION
------------------------------------
A service with 73 dependents reduced to 20 inside a sampled subgraph is a
different structural object. Every report here carries global degree, retained
degree, retention fraction and the observability cap imposed by subgraph size,
so the two are never confused.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .graph import Graph, from_edges
from .realtopo import DEFAULT_EDGES_CSV, _adjacency, load_real_edges

# Pools in fixed order. Every deterministic tie-break in this module resolves
# in this order, so results never depend on dict iteration or set ordering.
POOLS: tuple[str, ...] = ("train", "val", "test")
DEFAULT_FRACTIONS: tuple[float, float, float] = (0.50, 0.20, 0.30)

# Tier boundaries on GLOBAL in-degree ("how many services depend on me").
# Sizes on the ChronoGraph graph: 11 / 19 / 79 / 599.
TIER_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("T1", 30, 10**9),
    ("T2", 10, 29),
    ("T3", 3, 9),
    ("T4", 0, 2),
)

MAX_POOL_DEVIATION = 0.07


def largest_remainder(total: int, shares: tuple[float, ...]) -> tuple[int, ...]:
    """Allocate `total` indivisible items across pools by largest remainder.

    Ordinary rounding does not work: for a tier of 11 with shares
    (0.50, 0.20, 0.30) it gives round(5.5)+round(2.2)+round(3.3) = 6+2+3 = 11
    by luck, but for other sizes it over- or under-allocates and the quotas
    stop summing to the tier size. Largest remainder (Hamilton's method)
    always sums exactly: floor every exact share, then hand the leftover seats
    to the largest fractional parts, ties resolved by POOLS order.

    Worked examples on the real tier sizes:
        11 -> exact (5.5, 2.2, 3.3) -> floors (5,2,3)=10, 1 left -> train  -> (6,2,3)
        19 -> exact (9.5, 3.8, 5.7) -> floors (9,3,5)=17, 2 left -> val,test -> (9,4,6)
        79 -> exact (39.5,15.8,23.7) -> floors (39,15,23)=77, 2 left       -> (39,16,24)
    """
    if total < 0:
        raise ValueError("total must be non-negative")
    s = float(sum(shares))
    if s <= 0:
        raise ValueError("shares must sum to a positive value")
    exact = [total * (x / s) for x in shares]
    floors = [int(np.floor(v)) for v in exact]
    leftover = total - sum(floors)
    # Order by descending fractional remainder; ties by POOLS order (index).
    remainders = sorted(
        range(len(shares)), key=lambda i: (-(exact[i] - floors[i]), i)
    )
    for k in range(leftover):
        floors[remainders[k % len(floors)]] += 1
    assert sum(floors) == total, "largest_remainder failed to conserve total"
    return tuple(floors)


def _deal_sequence(quota: tuple[int, ...]) -> list[int]:
    """Interleave pool slots so large hubs do not all land in one pool.

    At each step the pool with the greatest UNUSED FRACTION of its quota takes
    the next item, ties by POOLS order. Dealing greedily by absolute remaining
    quota instead would send the biggest hubs to the biggest pool every time,
    and the rotation that is supposed to vary which hubs are held out would
    have no effect on the largest ones.
    """
    remaining = list(quota)
    seq: list[int] = []
    for _ in range(sum(quota)):
        best, best_key = -1, None
        for i, rem in enumerate(remaining):
            if rem <= 0 or quota[i] == 0:
                continue
            key = rem / quota[i]
            if best_key is None or key > best_key + 1e-12:
                best, best_key = i, key
        if best < 0:
            break
        remaining[best] -= 1
        seq.append(best)
    return seq


@dataclass
class HubRecord:
    """Everything needed to tell hub PRESENCE from hub PRESERVATION."""

    node: int
    tier: str
    global_in_degree: int
    pool: str
    retained_in_degree: int
    cut_edges: int

    @property
    def retention_fraction(self) -> float:
        return self.retained_in_degree / max(self.global_in_degree, 1)

    def observability_cap(self, max_subgraph_nodes: int) -> int:
        """A node cannot show in-degree k inside a subgraph of fewer than k+1."""
        return min(max_subgraph_nodes - 1, self.retained_in_degree)


@dataclass
class Partition:
    seed: int
    pools: dict[str, np.ndarray]
    hubs: list[HubRecord]
    deviation: dict[str, float]
    feasible: bool
    tier_quota: dict[str, tuple[int, ...]] = field(default_factory=dict)

    def pool_of(self, node: int) -> str | None:
        for name, arr in self.pools.items():
            if node in arr:
                return name
        return None

    def hubs_in(self, pool: str, tier: str | None = None) -> list[HubRecord]:
        return [h for h in self.hubs if h.pool == pool and (tier is None or h.tier == tier)]


def _global_in_degree(src_i: np.ndarray, dst_i: np.ndarray, n: int) -> np.ndarray:
    """How many services depend on each service (ChronoGraph edge = src depends on dst)."""
    deg = np.zeros(n, dtype=np.int64)
    for b in dst_i.tolist():
        deg[b] += 1
    return deg


def stratified_split_node_pools(
    partition_seed: int,
    fractions: tuple[float, float, float] = DEFAULT_FRACTIONS,
    path: Path | str = DEFAULT_EDGES_CSV,
    max_deviation: float = MAX_POOL_DEVIATION,
) -> Partition:
    """
    Deterministic hub-stratified partition. Same seed -> identical pools.

    Phase 1  tiered hub dealing, quotas by largest remainder, order rotated by
             the seed so a different hub is held out each time.
    Phase 2  dependent claiming: a hub keeps every dependent not already taken
             by a larger hub. Hub integrity outranks pool balance.
    Phase 3  residual fill, steering pools back towards their targets.
    Phase 4  structural feasibility gate on pool-size deviation.
    """
    src, dst = load_real_edges(path)
    nodes = sorted(set(src) | set(dst))
    index = {name: i for i, name in enumerate(nodes)}
    n = len(nodes)
    src_i = np.array([index[a] for a in src], dtype=np.int64)
    dst_i = np.array([index[b] for b in dst], dtype=np.int64)
    in_deg = _global_in_degree(src_i, dst_i, n)
    adj = _adjacency(src_i, dst_i, n)

    # Dependents of each service: who depends on me.
    dependents: dict[int, list[int]] = {}
    for a, b in zip(src_i.tolist(), dst_i.tolist()):
        dependents.setdefault(b, []).append(a)

    assignment: dict[int, int] = {}
    targets = [f * n for f in fractions]
    tier_quota: dict[str, tuple[int, ...]] = {}
    hub_records: list[HubRecord] = []

    # ---- Phase 1 + 2 -------------------------------------------------
    for tier_name, lo, hi in TIER_BOUNDS:
        members = [i for i in range(n) if lo <= in_deg[i] <= hi]
        if not members:
            tier_quota[tier_name] = tuple(0 for _ in POOLS)
            continue
        members.sort(key=lambda i: (-int(in_deg[i]), i))
        if tier_name == "T4":
            # The long tail is not stratified; Phase 3 places it.
            tier_quota[tier_name] = tuple(0 for _ in POOLS)
            continue

        quota = largest_remainder(len(members), fractions)
        tier_quota[tier_name] = quota
        rot = partition_seed % len(members)
        rotated = members[rot:] + members[:rot]
        seq = _deal_sequence(quota)

        for hub, pool_idx in zip(rotated, seq):
            if hub not in assignment:
                assignment[hub] = pool_idx
            # Phase 2: claim dependents for this hub's pool.
            retained = 0
            cut = 0
            for d in sorted(dependents.get(hub, [])):
                if d not in assignment:
                    assignment[d] = assignment[hub]
                    retained += 1
                elif assignment[d] == assignment[hub]:
                    retained += 1
                else:
                    cut += 1
            hub_records.append(
                HubRecord(
                    node=hub,
                    tier=tier_name,
                    global_in_degree=int(in_deg[hub]),
                    pool=POOLS[assignment[hub]],
                    retained_in_degree=retained,
                    cut_edges=cut,
                )
            )

    # ---- Phase 3: residual fill, BFS-contiguous, deficit-steered ------
    counts = [sum(1 for v in assignment.values() if v == i) for i in range(len(POOLS))]
    frontier: list[tuple[int, int, int]] = []
    for node, pool_idx in assignment.items():
        for nb in sorted(adj[node]):
            if nb not in assignment:
                heapq.heappush(frontier, (0, pool_idx, nb))

    unassigned = [i for i in range(n) if i not in assignment]
    while len(assignment) < n:
        placed = False
        while frontier:
            d, pool_idx, node = heapq.heappop(frontier)
            if node in assignment:
                continue
            # Only grow a pool that is still below its target.
            if counts[pool_idx] >= targets[pool_idx]:
                deficits = [targets[i] - counts[i] for i in range(len(POOLS))]
                pool_idx = int(np.argmax(deficits))
            assignment[node] = pool_idx
            counts[pool_idx] += 1
            for nb in sorted(adj[node]):
                if nb not in assignment:
                    heapq.heappush(frontier, (d + 1, pool_idx, nb))
            placed = True
            break
        if not placed:
            # Disconnected remainder: place deterministically into the pool
            # furthest below target.
            remaining = [i for i in unassigned if i not in assignment]
            if not remaining:
                break
            node = min(remaining)
            deficits = [targets[i] - counts[i] for i in range(len(POOLS))]
            pool_idx = int(np.argmax(deficits))
            assignment[node] = pool_idx
            counts[pool_idx] += 1
            for nb in sorted(adj[node]):
                if nb not in assignment:
                    heapq.heappush(frontier, (1, pool_idx, nb))

    pools = {
        name: np.array(
            sorted(k for k, v in assignment.items() if v == i), dtype=np.int64
        )
        for i, name in enumerate(POOLS)
    }
    deviation = {
        name: abs(len(pools[name]) / n - fractions[i]) for i, name in enumerate(POOLS)
    }
    feasible = max(deviation.values()) <= max_deviation

    return Partition(
        seed=partition_seed,
        pools=pools,
        hubs=hub_records,
        deviation=deviation,
        feasible=feasible,
        tier_quota=tier_quota,
    )


def feasible_partitions(
    count: int,
    fractions: tuple[float, float, float] = DEFAULT_FRACTIONS,
    path: Path | str = DEFAULT_EDGES_CSV,
    max_scan: int = 128,
) -> tuple[list[Partition], list[int]]:
    """Draw seeds in ascending order until `count` feasible partitions exist.

    Rejection depends only on the graph and the seed - never on a model
    output - so skipping an infeasible seed cannot bias a model comparison.
    Returns the accepted partitions and the list of rejected seeds.
    """
    accepted: list[Partition] = []
    rejected: list[int] = []
    for p in range(max_scan):
        if len(accepted) >= count:
            break
        part = stratified_split_node_pools(p, fractions, path)
        (accepted if part.feasible else rejected).append(part if part.feasible else p)
    return accepted, rejected


# =====================================================================
# Subgraph sampling: TEST-R (ordinary) and TEST-H (hub-seeded) kept apart
# =====================================================================
@dataclass
class SampleConfig:
    nodes_per_subgraph: tuple[int, int] = (40, 120)
    min_edges: int = 25
    max_attempts_factor: int = 40


def sample_pool_subgraphs(
    pool: np.ndarray,
    seed: int,
    count: int,
    cfg: SampleConfig,
    hub_seeded: bool = False,
    hub_nodes: list[int] | None = None,
    path: Path | str = DEFAULT_EDGES_CSV,
) -> list[tuple[list[dict], list[dict], set[int]]]:
    """
    Grow connected subgraphs inside one pool.

    `hub_seeded=False` draws seed nodes uniformly from the pool - this is the
    PRIMARY evaluation set (TEST-R). `hub_seeded=True` always starts at an
    assigned hub - the SECONDARY stress set (TEST-H). The two are produced by
    separate calls and must never be pooled: mixing them yields a distribution
    whose composition was chosen by the experimenter.

    Returns (components, dependencies, global_node_ids) so callers can report
    exactly which services each subgraph covers.
    """
    src, dst = load_real_edges(path)
    nodes = sorted(set(src) | set(dst))
    index = {name: i for i, name in enumerate(nodes)}
    src_i = np.array([index[a] for a in src], dtype=np.int64)
    dst_i = np.array([index[b] for b in dst], dtype=np.int64)
    n = len(nodes)

    allowed = np.zeros(n, dtype=bool)
    allowed[pool] = True
    adj = [{x for x in nb if allowed[x]} for nb in _adjacency(src_i, dst_i, n)]

    if hub_seeded:
        seeds_pool = [h for h in (hub_nodes or []) if allowed[h]]
        if not seeds_pool:
            return []
    else:
        seeds_pool = np.nonzero(allowed)[0].tolist()

    rng = np.random.default_rng(seed)
    out: list[tuple[list[dict], list[dict], set[int]]] = []
    attempts = 0
    while len(out) < count and attempts < count * cfg.max_attempts_factor:
        attempts += 1
        start = int(rng.choice(seeds_pool))
        target = int(rng.integers(cfg.nodes_per_subgraph[0], cfg.nodes_per_subgraph[1] + 1))

        used = ~allowed.copy()
        chosen = [start]
        used[start] = True
        frontier = [start]
        while frontier and len(chosen) < target:
            node = frontier.pop(0)
            nbrs = [x for x in adj[node] if not used[x]]
            rng.shuffle(nbrs)
            for nb in nbrs:
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

        components, dependencies = induced_records(
            sorted(keep), src_i[mask].tolist(), dst_i[mask].tolist(), rng
        )
        out.append((components, dependencies, keep))
    return out


def induced_records(
    global_nodes: list[int],
    sub_src: list[int],
    sub_dst: list[int],
    rng: np.random.Generator,
) -> tuple[list[dict], list[dict]]:
    """Build component/dependency records for an induced subgraph.

    Component type comes from the service's structural role INSIDE the
    subgraph; criticality is drawn independently of degree, so the attribute
    cannot manufacture a signal that predicts blast radius.
    """
    from .realtopo import CRITICALITY_MARGINAL, CRITICALITY_P, RealTopoConfig
    from .synth import _pick_dependency_type

    cfg = RealTopoConfig()
    local = {g: k for k, g in enumerate(global_nodes)}
    m = len(local)
    depends_on = np.zeros(m)
    dependents = np.zeros(m)
    for a, b in zip(sub_src, sub_dst):
        depends_on[local[int(a)]] += 1
        dependents[local[int(b)]] += 1

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
                "criticality": str(rng.choice(CRITICALITY_MARGINAL, p=CRITICALITY_P)),
                "is_active": 1,
            }
        )

    type_of = [c["component_type"] for c in components]
    dependencies: list[dict] = []
    for a, b in zip(sub_src, sub_dst):
        u, v = local[int(a)], local[int(b)]
        dep_type, critical = _pick_dependency_type(rng, type_of[v], type_of[u])
        dependencies.append(
            {
                "component_id": u + 1,
                "depends_on_id": v + 1,
                "dependency_type": dep_type,
                "is_critical": critical,
            }
        )
    return components, dependencies


def build_graph(components: list[dict], dependencies: list[dict]) -> Graph:
    """Feature construction, through the same path the full experiment uses."""
    return from_edges(components, dependencies)
