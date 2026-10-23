"""
Validation invariants for the corrected topology protocol.

These tests check the EVALUATION SUBSTRATE, not model quality. No model score
appears in any assertion, so nothing here can be used - deliberately or
accidentally - to select a model configuration.

Every check exercises the code path the full experiment will use:
ml/partition.py for partitioning and sampling, ml/graph.from_edges for feature
construction, ml/synth.simulate_cascade for labels.
"""

from __future__ import annotations

import numpy as np
import pytest

from ml.graph import from_edges
from ml.partition import (
    DEFAULT_FRACTIONS,
    MAX_POOL_DEVIATION,
    POOLS,
    SampleConfig,
    build_graph,
    induced_records,
    largest_remainder,
    sample_pool_subgraphs,
    stratified_split_node_pools,
)
from ml.realtopo import DEFAULT_EDGES_CSV, load_real_edges

TOTAL_SERVICES = 708
REAL_TIER_SIZES = {"T1": 11, "T2": 19, "T3": 79}


def _require_data():
    if not DEFAULT_EDGES_CSV.exists():
        pytest.skip(f"{DEFAULT_EDGES_CSV} missing; see ml/data/chronograph/SOURCE.md")


# ---------------------------------------------------------------------
# Quota allocation (design correction 1)
# ---------------------------------------------------------------------
@pytest.mark.parametrize("total", [11, 19, 79, 0, 1, 2, 3, 7, 100, 599, 708])
def test_quotas_sum_to_exact_tier_size(total):
    """Largest remainder must conserve the total for ANY tier size.

    Ordinary rounding does not: it can over- or under-allocate, silently
    losing or duplicating services at the tier boundary.
    """
    q = largest_remainder(total, DEFAULT_FRACTIONS)
    assert sum(q) == total
    assert len(q) == len(POOLS)
    assert all(x >= 0 for x in q)


def test_quotas_for_the_three_real_tiers():
    """Pin the exact allocation for the tier sizes that actually occur."""
    assert largest_remainder(11, DEFAULT_FRACTIONS) == (6, 2, 3)
    assert largest_remainder(19, DEFAULT_FRACTIONS) == (9, 4, 6)
    assert largest_remainder(79, DEFAULT_FRACTIONS) == (39, 16, 24)


def test_quota_allocation_is_deterministic_and_tie_broken_by_pool_order():
    """Equal shares on an indivisible total must resolve by POOLS order."""
    assert largest_remainder(10, DEFAULT_FRACTIONS) == largest_remainder(10, DEFAULT_FRACTIONS)
    # 4 items, equal thirds -> floors (1,1,1), one left over -> train.
    assert largest_remainder(4, (1 / 3, 1 / 3, 1 / 3)) == (2, 1, 1)


def test_quota_rejects_degenerate_shares():
    with pytest.raises(ValueError):
        largest_remainder(10, (0.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        largest_remainder(-1, DEFAULT_FRACTIONS)


# ---------------------------------------------------------------------
# V1 / V2 / V3 / V6 / V7 / V8  - partition invariants
# ---------------------------------------------------------------------
def test_v1_pools_are_disjoint_and_cover_every_service():
    _require_data()
    part = stratified_split_node_pools(0)
    sets = [set(part.pools[p].tolist()) for p in POOLS]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            assert not (sets[i] & sets[j]), f"{POOLS[i]} and {POOLS[j]} overlap"
    assert len(set().union(*sets)) == TOTAL_SERVICES


def test_v2_partition_is_deterministic():
    _require_data()
    a = stratified_split_node_pools(3)
    b = stratified_split_node_pools(3)
    for p in POOLS:
        assert np.array_equal(a.pools[p], b.pools[p])


def test_v2b_different_seeds_move_hubs_between_pools():
    """Rotation must actually change WHICH hubs are held out.

    If every seed produced the same hub assignment, varying the partition seed
    would add nothing over the old hardcoded split.
    """
    _require_data()
    rosters = set()
    for p in range(6):
        part = stratified_split_node_pools(p)
        rosters.add(tuple(sorted(h.node for h in part.hubs_in("test", "T1"))))
    assert len(rosters) > 1, "test-pool T1 hub roster never changes across seeds"


def test_v3_no_hub_appears_in_two_pools():
    _require_data()
    part = stratified_split_node_pools(0)
    seen: dict[int, str] = {}
    for h in part.hubs:
        if h.node in seen:
            assert seen[h.node] == h.pool, f"hub {h.node} in two pools"
        seen[h.node] = h.pool


def test_v6_hub_dependents_share_the_hub_pool_or_are_counted_as_cut():
    """Hub integrity: a claimed dependent is in the hub's pool; anything
    elsewhere is counted as a cut edge, never duplicated."""
    _require_data()
    part = stratified_split_node_pools(1)
    src, dst = load_real_edges()
    nodes = sorted(set(src) | set(dst))
    index = {n: i for i, n in enumerate(nodes)}
    pool_of = {}
    for name in POOLS:
        for node in part.pools[name].tolist():
            pool_of[node] = name

    for h in part.hubs:
        deps = [index[a] for a, b in zip(src, dst) if index[b] == h.node]
        same = sum(1 for d in deps if pool_of[d] == h.pool)
        other = sum(1 for d in deps if pool_of[d] != h.pool)
        assert same == h.retained_in_degree, f"hub {h.node} retained mismatch"
        assert other == h.cut_edges, f"hub {h.node} cut mismatch"


def test_v5_edge_accounting_is_complete():
    """retained + cut == global incident edges, for every hub."""
    _require_data()
    part = stratified_split_node_pools(2)
    for h in part.hubs:
        assert h.retained_in_degree + h.cut_edges == h.global_in_degree, (
            f"hub {h.node}: {h.retained_in_degree}+{h.cut_edges} != {h.global_in_degree}"
        )
        assert 0.0 <= h.retention_fraction <= 1.0


def test_v7_pool_deviation_is_measured_and_gate_is_consistent():
    """The feasibility gate must agree with the measured deviation."""
    _require_data()
    for p in range(6):
        part = stratified_split_node_pools(p)
        worst = max(part.deviation.values())
        assert part.feasible == (worst <= MAX_POOL_DEVIATION)


def test_v8_every_pool_receives_hubs_from_the_top_tiers():
    _require_data()
    part = stratified_split_node_pools(0)
    for pool in POOLS:
        assert len(part.hubs_in(pool, "T1")) >= 1, f"{pool} has no T1 hub"
        assert len(part.hubs_in(pool, "T2")) >= 2, f"{pool} has fewer than 2 T2 hubs"


def test_tier_quotas_match_the_real_tier_sizes():
    _require_data()
    part = stratified_split_node_pools(0)
    for tier, size in REAL_TIER_SIZES.items():
        assert sum(part.tier_quota[tier]) == size, f"{tier} quota does not sum to {size}"


# ---------------------------------------------------------------------
# V4' - feature isolation, by invariance rather than by numeric difference
# ---------------------------------------------------------------------
def test_v4_external_edges_cannot_affect_induced_subgraph_features():
    """
    The invariant the pool split depends on.

    The previous check asserted that a hub's local encoding DIFFERS from its
    global-degree encoding. That tests an incidental numeric consequence - the
    two also differ because `in_degree_norm` divides by log1p(N-1) with N the
    subgraph size - so it can pass on a normalisation constant alone while
    proving nothing about edge isolation.

    This asserts the property directly: add arbitrarily many edges with one
    endpoint inside the node set and one outside, and every array produced by
    the real feature path must be bitwise identical.
    """
    _require_data()
    src, dst = load_real_edges()
    nodes = sorted(set(src) | set(dst))
    index = {n: i for i, n in enumerate(nodes)}
    src_i = np.array([index[a] for a in src], dtype=np.int64)
    dst_i = np.array([index[b] for b in dst], dtype=np.int64)
    n = len(nodes)

    part = stratified_split_node_pools(0)
    cfg = SampleConfig(nodes_per_subgraph=(20, 60), min_edges=10)
    samples = sample_pool_subgraphs(part.pools["test"], seed=5, count=3, cfg=cfg)
    assert samples, "no subgraph sampled; cannot test the invariant"

    checked = 0
    for _components, _deps, keep in samples:
        inside = sorted(keep)
        outside = [i for i in range(n) if i not in keep]

        def induced(extra_src, extra_dst):
            s = np.concatenate([src_i, np.asarray(extra_src, dtype=np.int64)])
            d = np.concatenate([dst_i, np.asarray(extra_dst, dtype=np.int64)])
            m = np.array(
                [(int(a) in keep and int(b) in keep) for a, b in zip(s, d)], dtype=bool
            )
            rng = np.random.default_rng(0)      # identical attribute draws
            comps, deps = induced_records(inside, s[m].tolist(), d[m].tolist(), rng)
            return build_graph(comps, deps)

        base = induced([], [])
        rng = np.random.default_rng(7)
        for k in (1, 5, 25):
            for _trial in range(3):
                ext_in = rng.choice(inside, size=k).tolist()
                ext_out = rng.choice(outside, size=k).tolist()
                # both directions of external attachment
                g1 = induced(ext_in, ext_out)
                g2 = induced(ext_out, ext_in)
                for g in (g1, g2):
                    assert np.array_equal(g.node_features, base.node_features)
                    assert np.array_equal(g.edge_features, base.edge_features)
                    assert np.array_equal(g.src, base.src)
                    assert np.array_equal(g.dst, base.dst)
                    assert np.array_equal(g.node_ids, base.node_ids)
                checked += 2
    assert checked >= 18, f"only {checked} perturbations exercised"


def test_from_edges_is_a_pure_function_of_its_inputs():
    """Feature construction must not depend on call order or hidden state."""
    comps = [
        {"component_id": i, "component_name": f"C{i}", "component_type": "Service",
         "criticality": "High", "is_active": 1}
        for i in (1, 2, 3)
    ]
    deps = [
        {"component_id": 2, "depends_on_id": 1, "dependency_type": "Synchronous",
         "is_critical": 1},
        {"component_id": 3, "depends_on_id": 2, "dependency_type": "Asynchronous",
         "is_critical": 0},
    ]
    a, b = from_edges(comps, deps), from_edges(comps, deps)
    assert np.array_equal(a.node_features, b.node_features)
    assert np.array_equal(a.edge_features, b.edge_features)


# ---------------------------------------------------------------------
# V10 / V11 - evaluation-set separation and observability
# ---------------------------------------------------------------------
def test_v10_ordinary_and_hub_seeded_sets_are_produced_separately():
    """
    TEST-R and TEST-H must be distinct objects built by separate calls.

    Pooling them would produce a distribution whose hub content was chosen by
    the experimenter rather than sampled.
    """
    _require_data()
    part = stratified_split_node_pools(0)
    cfg = SampleConfig(nodes_per_subgraph=(20, 60), min_edges=10)
    hubs = [h.node for h in part.hubs_in("test")]

    test_r = sample_pool_subgraphs(part.pools["test"], 11, 4, cfg, hub_seeded=False)
    test_h = sample_pool_subgraphs(
        part.pools["test"], 11, 4, cfg, hub_seeded=True, hub_nodes=hubs
    )
    assert test_r and test_h

    # TEST-H subgraphs must each contain at least one assigned test hub.
    for _c, _d, keep in test_h:
        assert keep & set(hubs), "hub-seeded subgraph contains no assigned hub"

    # The two sets must not be identical objects.
    r_sets = [frozenset(k) for _c, _d, k in test_r]
    h_sets = [frozenset(k) for _c, _d, k in test_h]
    assert r_sets != h_sets


def test_v11_observability_cap_is_respected():
    """A hub cannot show in-degree k inside a subgraph of fewer than k+1 nodes."""
    _require_data()
    part = stratified_split_node_pools(0)
    for h in part.hubs:
        for max_nodes in (40, 120):
            assert h.observability_cap(max_nodes) <= max_nodes - 1
            assert h.observability_cap(max_nodes) <= h.retained_in_degree


# ---------------------------------------------------------------------
# V9 - cascade legality through the real simulation path
# ---------------------------------------------------------------------
def test_v9_simulated_cascades_respect_the_induced_graph():
    _require_data()
    from ml.realtopo import _truth_for
    from ml.synth import SynthConfig, simulate_cascade

    part = stratified_split_node_pools(0)
    cfg = SampleConfig(nodes_per_subgraph=(20, 60), min_edges=10)
    samples = sample_pool_subgraphs(part.pools["test"], 13, 3, cfg)
    assert samples

    scfg = SynthConfig(false_positive_rate=0.0)   # isolate the mechanism
    rng = np.random.default_rng(0)
    for components, deps, _keep in samples:
        graph = build_graph(components, deps)
        truth = _truth_for(graph, deps, rng, scfg)
        for _ in range(3):
            root = int(rng.integers(0, graph.num_nodes))
            cas = simulate_cascade(rng, graph, truth, root, scfg)
            affected = cas.labels > 0.5
            affected[root] = False
            assert not (affected & ~graph.reachable_from(root)).any()
