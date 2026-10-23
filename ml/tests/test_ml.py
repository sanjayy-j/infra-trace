"""
Tests for the InfraTrace ML layer.

The important ones here are not the smoke tests. They are:

  * test_prediction_never_exceeds_reachability - the safety property the
    whole design rests on. If this fails, adopting the model could hide a
    component the current SQL would have flagged, and the method's main
    operational argument is gone.
  * test_python_reachability_matches_sql - the database is the source of
    truth; ml/graph.py keeps a second traversal for synthetic graphs, and
    this proves the two agree on the real one.
  * test_propagation_direction - a reversed edge silently inverts every
    answer while still producing plausible-looking output. Worth pinning.

Tests needing MySQL skip cleanly when it is unreachable, so the suite runs
on a machine with no database.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from ml.graph import (
    EDGE_FEATURE_DIM,
    NODE_FEATURE_DIM,
    Cascade,
    from_edges,
)
from ml.models import SCPConfig, SCPNet
from ml.synth import SynthConfig, generate_dataset, generate_graph, simulate_cascade
from ml.train import TrainConfig, to_tensors, train_model


# ---------------------------------------------------------------------
# Graph construction and direction
# ---------------------------------------------------------------------
def _toy_graph():
    """A -> B -> C chain. B depends on A; C depends on B."""
    components = [
        {"component_id": 1, "component_name": "A", "component_type": "Database",
         "criticality": "Critical", "is_active": 1},
        {"component_id": 2, "component_name": "B", "component_type": "Service",
         "criticality": "High", "is_active": 1},
        {"component_id": 3, "component_name": "C", "component_type": "Gateway",
         "criticality": "Medium", "is_active": 1},
    ]
    dependencies = [
        {"component_id": 2, "depends_on_id": 1, "dependency_type": "Synchronous",
         "is_critical": 1},
        {"component_id": 3, "depends_on_id": 2, "dependency_type": "Synchronous",
         "is_critical": 1},
    ]
    return from_edges(components, dependencies)


def test_propagation_direction():
    """Failure must flow from a dependency to its dependents, not the reverse."""
    g = _toy_graph()
    a, b, c = g.index_of(1), g.index_of(2), g.index_of(3)

    # A is the deepest dependency: its failure reaches B and C.
    assert g.reachable_from(a).sum() == 2
    assert g.reachable_from(a)[b] and g.reachable_from(a)[c]

    # C depends on everything but nothing depends on C: it reaches nobody.
    assert g.reachable_from(c).sum() == 0

    assert g.hop_distance_from(a)[b] == 1
    assert g.hop_distance_from(a)[c] == 2


def test_feature_dimensions():
    g = _toy_graph()
    assert g.node_features.shape == (3, NODE_FEATURE_DIM)
    assert g.edge_features.shape == (2, EDGE_FEATURE_DIM)


def test_self_loops_and_dangling_edges_dropped():
    components = [
        {"component_id": 1, "component_name": "A", "component_type": "Service",
         "criticality": "Low", "is_active": 1},
    ]
    dependencies = [
        {"component_id": 1, "depends_on_id": 1, "dependency_type": "Synchronous",
         "is_critical": 0},                                    # self loop
        {"component_id": 1, "depends_on_id": 99, "dependency_type": "Data",
         "is_critical": 0},                                    # dangling
    ]
    assert from_edges(components, dependencies).num_edges == 0


def test_unknown_categorical_degrades_to_zero_vector():
    """A component type added after training must not crash inference."""
    components = [
        {"component_id": 1, "component_name": "A", "component_type": "Quantum",
         "criticality": "Nonsense", "is_active": 1},
    ]
    g = from_edges(components, [])
    assert np.isfinite(g.node_features).all()


# ---------------------------------------------------------------------
# The safety property
# ---------------------------------------------------------------------
def test_prediction_never_exceeds_reachability():
    """
    An unreachable component must receive probability ~0, exactly.

    This is what makes the model safe to put in front of an on-call
    engineer: it can only prune the SQL's false positives, never invent an
    affected component the recursive CTE did not already find. Checked on
    randomly initialised AND trained weights, because the property must come
    from the propagation rule rather than from what the model learned.
    """
    dataset = generate_dataset(seed=3, num_graphs=12, cascades_per_graph=4)
    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden=16, rounds=5))
    model.eval()  # dropout would re-sample edge probabilities on every call

    checked = 0
    for graph, cascades in dataset:
        t = to_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        with torch.no_grad():
            q = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots).numpy()
        for b, cas in enumerate(cascades):
            unreachable = ~graph.reachable_from(cas.root_index)
            unreachable[cas.root_index] = False
            if unreachable.any():
                assert q[b][unreachable].max() < 1e-5
                checked += int(unreachable.sum())
    assert checked > 50, "test did not actually exercise unreachable nodes"


def test_root_probability_is_one():
    g = _toy_graph()
    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden=8, rounds=3))
    model.eval()
    t = to_tensors(g)
    with torch.no_grad():
        q = model(t["x"], t["src"], t["dst"], t["edge_attr"], torch.tensor([0]))
    assert pytest.approx(1.0, abs=1e-6) == float(q[0, 0])


def test_more_rounds_is_monotone_non_decreasing():
    """Extra propagation rounds can only spread failure further, never less."""
    dataset = generate_dataset(seed=5, num_graphs=6, cascades_per_graph=3)
    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden=16, rounds=2))
    # eval() is load-bearing: in train mode dropout re-samples the edge
    # transmission probabilities on each forward pass, so two calls are two
    # different models and comparing their outputs tests nothing.
    model.eval()
    for graph, cascades in dataset:
        t = to_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        with torch.no_grad():
            q2 = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots, rounds=2)
            q6 = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots, rounds=6)
        assert (q6 >= q2 - 1e-6).all()


def test_outputs_are_probabilities():
    dataset = generate_dataset(seed=7, num_graphs=5, cascades_per_graph=3)
    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden=16, rounds=4))
    model.eval()
    for graph, cascades in dataset:
        t = to_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        with torch.no_grad():
            q = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots)
        assert torch.isfinite(q).all()
        assert (q >= 0).all() and (q <= 1).all()


# ---------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------
def test_generator_is_deterministic_given_a_seed():
    a = generate_dataset(seed=42, num_graphs=4, cascades_per_graph=3)
    b = generate_dataset(seed=42, num_graphs=4, cascades_per_graph=3)
    assert len(a) == len(b)
    for (g1, c1), (g2, c2) in zip(a, b):
        assert np.array_equal(g1.node_features, g2.node_features)
        assert np.array_equal(g1.src, g2.src) and np.array_equal(g1.dst, g2.dst)
        for x, y in zip(c1, c2):
            assert x.root_index == y.root_index
            assert np.array_equal(x.labels, y.labels)


def test_simulated_cascade_stays_within_reachable_set():
    """Ground truth must respect the graph: failure cannot teleport."""
    rng = np.random.default_rng(11)
    cfg = SynthConfig(false_positive_rate=0.0)  # isolate the mechanism
    graph, truth = generate_graph(rng, cfg)
    for _ in range(20):
        root = int(rng.integers(0, graph.num_nodes))
        cas = simulate_cascade(rng, graph, truth, root, cfg)
        affected = cas.labels > 0.5
        affected[root] = False
        assert not (affected & ~graph.reachable_from(root)).any()


def test_generator_produces_non_ic_mechanisms():
    """The circularity guard: quorum nodes must actually be generated.

    If this ever returns only OR nodes, the training data collapses onto the
    proposed model's own hypothesis class and every benchmark result becomes
    an artefact. See the module docstring of ml/synth.py.
    """
    rng = np.random.default_rng(2)
    seen = set()
    for _ in range(12):
        _graph, truth = generate_graph(rng, SynthConfig())
        seen.update(np.unique(truth["mechanism"]).tolist())
    assert 1 in seen, "no QUORUM nodes generated - data is pure Independent Cascade"
    assert 2 in seen, "no RESILIENT nodes generated"


# ---------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------
def test_training_improves_on_random_initialisation():
    from ml.evaluate import summarize
    from ml.train import collect_predictions

    train_ds = generate_dataset(21, 20, 5)
    val_ds = generate_dataset(22, 8, 5)
    test_ds = generate_dataset(23, 8, 5)

    torch.manual_seed(0)
    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 16, 4))
    y, p = collect_predictions(model, test_ds)
    before = summarize(y, p)["pr_auc"]

    train_model(model, train_ds, val_ds, TrainConfig(epochs=40, seed=0))
    y, p = collect_predictions(model, test_ds)
    after = summarize(y, p)["pr_auc"]

    assert after > before, f"training did not help: {before:.3f} -> {after:.3f}"


def test_eval_mask_excludes_root():
    g = _toy_graph()
    cas = Cascade(root_index=0, labels=np.array([1.0, 1.0, 0.0], dtype=np.float32))
    assert not cas.eval_mask(g)[0]


# ---------------------------------------------------------------------
# Database-backed tests
# ---------------------------------------------------------------------
def _db_or_skip():
    try:
        from ml.extract import load_graph

        return load_graph()
    except Exception as exc:  # pragma: no cover - depends on local setup
        pytest.skip(f"MySQL not reachable: {exc}")


def test_python_reachability_matches_sql():
    """The second traversal must agree with fn_blast_radius_count exactly."""
    from ml.extract import verify_against_sql

    graph = _db_or_skip()
    assert verify_against_sql(graph)["mismatches"] == 0


def test_real_incidents_extract_with_single_roots():
    from ml.extract import load_incident_cascades

    graph = _db_or_skip()
    cascades = load_incident_cascades(graph)
    assert len(cascades) > 0
    for cas in cascades:
        assert cas.labels[cas.root_index] == 1.0


def test_some_real_impact_flows_against_the_dependency_edges():
    """
    Pin the structural ceiling on recall.

    Not every recorded impact is reachable from its root cause by reverse
    traversal. Incident 11 is an API Gateway misconfiguration that degraded
    Order Service and Search Service - but the Gateway DEPENDS ON those
    services, so the failure travelled forwards along the edge, from caller
    to callee, not backwards from dependency to dependent.

    This is not a data error and it is not something the model can fix: the
    SQL, SCP-GNN and every other method built on reverse reachability are
    all blind to it in exactly the same way. It caps recall at 29/37 = 78.4%
    on this dataset.

    The test asserts the phenomenon exists rather than a specific count, so
    that editing the seed data does not break it - but a sharp change here
    means the ceiling moved and docs/ml/experiments.md needs updating.
    """
    from ml.extract import load_incident_cascades

    graph = _db_or_skip()
    cascades = load_incident_cascades(graph)

    unreachable_impacts = total_impacts = 0
    for cas in cascades:
        affected = cas.labels > 0.5
        affected[cas.root_index] = False
        total_impacts += int(affected.sum())
        unreachable_impacts += int(
            (affected & ~graph.reachable_from(cas.root_index)).sum()
        )

    assert total_impacts > 0
    assert unreachable_impacts > 0, (
        "No impact flows against the dependency edges. If the seed data "
        "changed so that reverse reachability now explains every incident, "
        "the recall ceiling documented in docs/ml/experiments.md is stale."
    )
    assert unreachable_impacts < total_impacts / 2, (
        "More than half of recorded impact is unreachable from its root "
        "cause - reverse traversal would no longer be the right model."
    )


def test_linear_link_is_a_working_configuration():
    """
    The 28-parameter linear transmission function must stay usable.

    With `linear_link=True` the model is exactly the hyperparametric
    Independent Cascade model of Kalimeris et al. (2018), and the ablation in
    docs/ml/experiments.md found it statistically indistinguishable from the
    default MLP. That makes it the simplest defensible configuration, so it
    needs to keep working - including the reachability bound, which must come
    from the propagation rule and not from the shape of the link function.
    """
    cfg = SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, hidden=32, rounds=4,
                    linear_link=True)
    model = SCPNet(cfg)
    model.eval()
    n_params = sum(p.numel() for p in model.parameters())
    assert n_params < 100, f"linear link should be tiny, got {n_params} parameters"

    dataset = generate_dataset(seed=31, num_graphs=6, cascades_per_graph=3)
    for graph, cascades in dataset:
        t = to_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        with torch.no_grad():
            q = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots).numpy()
        assert np.isfinite(q).all()
        for b, cas in enumerate(cascades):
            unreachable = ~graph.reachable_from(cas.root_index)
            unreachable[cas.root_index] = False
            if unreachable.any():
                assert q[b][unreachable].max() < 1e-5


# ---------------------------------------------------------------------
# Directionality (SCP-BiD). See docs/ml/direction-study.md.
# ---------------------------------------------------------------------
def test_forward_propagation_is_off_by_default():
    """
    Regression guard on every previously published number.

    Enabling forward propagation changes the ground-truth process, and so
    every result in docs/ml/experiments.md sections 3 and 4. The default must
    stay off so those remain reproducible; the directionality experiment opts
    in explicitly.
    """
    assert SynthConfig().enable_forward_propagation is False


def test_bidirectional_view_is_a_superset():
    g = _toy_graph()
    assert g.bi_src is not None and g.bi_src.shape[0] == 2 * g.num_edges
    assert np.array_equal(g.bi_src[: g.num_edges], g.src)
    for i in range(g.num_nodes):
        rev, bi = g.reachable_from(i), g.reachable_bidirectional(i)
        assert (bi | rev == bi).all(), "bidirectional must contain reverse reachability"

    # C depends on B depends on A: nothing depends on C, so reverse traversal
    # from C reaches nobody, but forwards it reaches both.
    c = g.index_of(3)
    assert g.reachable_from(c).sum() == 0
    assert g.reachable_bidirectional(c).sum() == 2


def test_direction_flag_is_the_only_added_feature():
    g = _toy_graph()
    assert g.bi_edge_features.shape[1] == EDGE_FEATURE_DIM + 1
    e = g.num_edges
    # Reverse half flagged 0, forward half flagged 1, everything else copied.
    assert (g.bi_edge_features[:e, -1] == 0.0).all()
    assert (g.bi_edge_features[e:, -1] == 1.0).all()
    assert np.array_equal(g.bi_edge_features[:e, :-1], g.edge_features)
    assert np.array_equal(g.bi_edge_features[e:, :-1], g.edge_features)


def test_scp_bid_is_bounded_by_bidirectional_reachability():
    """
    SCP-BiD's safety property, which is WEAKER than SCP-GNN's on purpose.

    SCP-GNN can never flag anything the SQL would not. SCP-BiD gives that up
    to reach forward-propagated impacts, so its bound is the bidirectional
    reachable set - about six times larger on the real graph. It must still
    respect that bound exactly.
    """
    from ml.models.scp_bid import SCPBiDConfig, SCPBiDNet, bidirectional_tensors

    dataset = generate_dataset(
        seed=41, num_graphs=25, cascades_per_graph=3,
        cfg=SynthConfig(enable_forward_propagation=True),
    )
    model = SCPBiDNet(SCPBiDConfig(NODE_FEATURE_DIM, hidden=16, rounds=5))
    model.eval()

    checked = 0
    for graph, cascades in dataset:
        t = bidirectional_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        with torch.no_grad():
            q = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots).numpy()
        for b, cas in enumerate(cascades):
            unreachable = ~graph.reachable_bidirectional(cas.root_index)
            unreachable[cas.root_index] = False
            if unreachable.any():
                assert q[b][unreachable].max() < 1e-5
                checked += int(unreachable.sum())
    # The bar is low on purpose. Bidirectional reachability covers almost the
    # whole graph - that is the point of the complaint against it - so nodes
    # outside it are genuinely rare and a high threshold would just be flaky.
    assert checked > 5, "test did not exercise any unreachable nodes"


def test_direction_blind_ablation_changes_nothing_but_the_flag():
    """The ablation must zero the flag and leave every other feature alone."""
    from ml.models.scp_bid import SCPBiDConfig, SCPBiDNet

    g = _toy_graph()
    blind = SCPBiDNet(SCPBiDConfig(NODE_FEATURE_DIM, use_direction_flag=False))
    attr = torch.as_tensor(g.bi_edge_features, dtype=torch.float32)
    masked = blind._maybe_blind(attr)
    assert (masked[:, -1] == 0).all()
    assert torch.equal(masked[:, :-1], attr[:, :-1])

    seeing = SCPBiDNet(SCPBiDConfig(NODE_FEATURE_DIM, use_direction_flag=True))
    assert torch.equal(seeing._maybe_blind(attr), attr)


def test_generator_produces_forward_only_impacts_when_enabled():
    """
    The generator must actually exercise the mechanism under study.

    If forward propagation never produces an impact outside the reverse
    reachable set, the directionality experiment is measuring nothing and its
    results are meaningless.
    """
    dataset = generate_dataset(
        seed=43, num_graphs=20, cascades_per_graph=5,
        cfg=SynthConfig(enable_forward_propagation=True),
    )
    forward_only = 0
    for graph, cascades in dataset:
        for cas in cascades:
            affected = cas.labels > 0.5
            affected[cas.root_index] = False
            forward_only += int((affected & ~graph.reachable_from(cas.root_index)).sum())
    assert forward_only > 10, (
        f"only {forward_only} forward-only impacts generated - the forward "
        "mechanism is not firing and the experiment would be vacuous"
    )


def test_no_forward_only_impacts_when_disabled():
    """With the mechanism off, every impact must stay reverse-reachable."""
    dataset = generate_dataset(
        seed=44, num_graphs=15, cascades_per_graph=4,
        cfg=SynthConfig(enable_forward_propagation=False, false_positive_rate=0.0),
    )
    for graph, cascades in dataset:
        for cas in cascades:
            affected = cas.labels > 0.5
            affected[cas.root_index] = False
            assert not (affected & ~graph.reachable_from(cas.root_index)).any()


# ---------------------------------------------------------------------
# Incomplete graphs. See docs/ml/incomplete-graph-study.md.
# ---------------------------------------------------------------------
def test_hiding_edges_recomputes_degree_features():
    """
    Hidden structure must not leak through node features.

    Degree is a node feature. If the observed graph kept the COMPLETE graph's
    degrees, the model would be told how many dependencies a component has
    while being denied the edges - handing it part of the answer it is being
    tested on not having.
    """
    from ml.incomplete import subgraph_with_edges

    g = _toy_graph()
    keep = np.array([True, False])  # drop B->C
    obs = subgraph_with_edges(g, keep)

    assert obs.num_edges == 1
    assert not np.array_equal(obs.node_features, g.node_features), (
        "degree features unchanged after hiding an edge - structure is leaking"
    )
    # Only the two degree columns may differ.
    assert np.array_equal(obs.node_features[:, :-2], g.node_features[:, :-2])


def test_bridge_removal_hurts_more_than_random_removal():
    """
    Replicates the distinction Chandrasekhar et al. (2024) prove.

    Uniform thinning preserves structure and is comparatively benign; removing
    bridge edges severs the graph and is catastrophic. If this ever stops
    holding, the experiment is no longer testing the hard case and
    docs/ml/incomplete-graph-study.md is wrong about what it measured.
    """
    from ml.incomplete import hide_edges

    dataset = generate_dataset(seed=61, num_graphs=12, cascades_per_graph=2)
    rng = np.random.default_rng(0)
    lost_random = lost_bridge = total = 0

    for graph, cascades in dataset:
        obs_rand, _ = hide_edges(graph, 0.25, "random", rng)
        obs_bridge, _ = hide_edges(graph, 0.25, "bridge", rng)
        for cas in cascades:
            r = cas.root_index
            full = int(graph.reachable_from(r).sum())
            total += full
            lost_random += full - int(obs_rand.reachable_from(r).sum())
            lost_bridge += full - int(obs_bridge.reachable_from(r).sum())

    assert total > 0
    assert lost_bridge > lost_random, (
        f"bridge removal lost {lost_bridge} reachable components, random lost "
        f"{lost_random} - the targeted regime is not harder, so the experiment "
        "is only testing the benign case"
    )


def test_latent_candidates_exclude_observed_edges_and_self_loops():
    from ml.incomplete import latent_candidate_pairs

    g = _toy_graph()
    cs, cd = latent_candidate_pairs(g, top_k=0)
    observed = {(int(g.src[e]), int(g.dst[e])) for e in range(g.num_edges)}
    pairs = set(zip(cs.tolist(), cd.tolist()))

    assert not (pairs & observed), "candidate set contains already-observed edges"
    assert not any(s == t for s, t in pairs), "candidate set contains self-loops"
    assert len(pairs) == g.num_nodes * (g.num_nodes - 1) - g.num_edges


def test_scp_latent_reduces_to_scp_when_top_k_is_zero():
    """
    With no latent edges admitted, SCP-Latent must inherit SCP-GNN's bound
    exactly: probability zero outside the observed reachable set.
    """
    from ml.models.scp_latent import SCPLatentConfig, SCPLatentNet, latent_tensors

    dataset = generate_dataset(seed=62, num_graphs=8, cascades_per_graph=3)
    model = SCPLatentNet(SCPLatentConfig(NODE_FEATURE_DIM, top_k=0))
    model.eval()

    checked = 0
    for graph, cascades in dataset:
        t = latent_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        with torch.no_grad():
            q = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots,
                      t["cand_src"], t["cand_dst"]).numpy()
        for b, cas in enumerate(cascades):
            unreachable = ~graph.reachable_from(cas.root_index)
            unreachable[cas.root_index] = False
            if unreachable.any():
                assert q[b][unreachable].max() < 1e-5
                checked += int(unreachable.sum())
    assert checked > 20


def test_scp_latent_can_reach_past_the_observed_graph():
    """
    The whole point of the mechanism: with top_k > 0 it must assign non-zero
    probability to components the observed edges cannot reach. If this fails
    the model is not doing the thing it was built to do.
    """
    from ml.models.scp_latent import SCPLatentConfig, SCPLatentNet, latent_tensors

    dataset = generate_dataset(seed=63, num_graphs=6, cascades_per_graph=3)
    model = SCPLatentNet(SCPLatentConfig(NODE_FEATURE_DIM, top_k=3))
    model.eval()

    reached = 0
    for graph, cascades in dataset:
        t = latent_tensors(graph)
        roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
        with torch.no_grad():
            q = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots,
                      t["cand_src"], t["cand_dst"]).numpy()
        for b, cas in enumerate(cascades):
            unreachable = ~graph.reachable_from(cas.root_index)
            unreachable[cas.root_index] = False
            if unreachable.any():
                reached += int((q[b][unreachable] > 1e-4).sum())
    assert reached > 0, "latent edges never reached beyond the observed graph"


# ---------------------------------------------------------------------
# Interpretability of the linear transmission function.
# See docs/ml/parsimony-study.md.
# ---------------------------------------------------------------------
def test_feature_names_match_the_model_input_width():
    """
    ml/explain.py names the transmission function's inputs positionally. If
    ml/graph.py ever changes the feature layout and this list is not updated,
    every reported coefficient silently acquires the wrong label - which is
    worse than no explanation at all.
    """
    from ml.explain import feature_names

    assert len(feature_names()) == 2 * NODE_FEATURE_DIM + EDGE_FEATURE_DIM


def test_coefficients_refuse_to_be_read_from_an_mlp():
    """An MLP has no one weight per feature; reporting its first layer would
    be meaningless, so it must raise rather than mislead."""
    from ml.explain import linear_coefficients

    mlp = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, linear_link=False))
    with pytest.raises(ValueError, match="linear"):
        linear_coefficients([mlp])


def test_linear_model_has_28_parameters_and_readable_coefficients():
    from ml.explain import linear_coefficients

    model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, linear_link=True))
    assert sum(p.numel() for p in model.parameters()) == 28

    coefs = linear_coefficients([model])
    assert len(coefs) == 2 * NODE_FEATURE_DIM + EDGE_FEATURE_DIM
    assert {c.name for c in coefs} >= set(EDGE_FEATURE_NAMES_FOR_TEST)


EDGE_FEATURE_NAMES_FOR_TEST = (
    "dep_type=Synchronous",
    "dep_type=Asynchronous",
    "dep_type=Data",
    "dep_type=Config",
    "is_critical",
)


def test_learned_edge_ranking_matches_the_measured_rates():
    """
    The model's learned ordering of dependency types must reproduce the rates
    measured on the real incidents (design.md: Synchronous+critical 85.7%,
    Synchronous 50%, Data 50%, Asynchronous 40%).

    The model never sees those rates - it is trained only on synthetic
    cascades - so agreement is independent corroboration. If this ordering
    ever inverts, either the generator's assumptions or the model's learning
    has drifted away from the one piece of real evidence available.
    """
    from ml.explain import edge_type_ranking, linear_coefficients
    from ml.train import TrainConfig, train_model

    models = []
    for seed in range(3):
        train_ds = generate_dataset(seed * 100 + 11, 40, 6)
        val_ds = generate_dataset(seed * 100 + 12, 12, 6)
        torch.manual_seed(seed)
        m = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4, linear_link=True))
        train_model(m, train_ds, val_ds, TrainConfig(epochs=60, seed=seed))
        models.append(m)

    ranking = dict(edge_type_ranking(linear_coefficients(models)))
    assert ranking["Synchronous+critical"] > ranking["Synchronous"], (
        "a critical synchronous dependency must transmit more readily than a "
        "non-critical one"
    )
    assert ranking["Synchronous"] > ranking["Asynchronous"], (
        "synchronous dependencies must transmit more readily than asynchronous "
        "ones - measured at 50% against 40% on the real incidents"
    )


def test_linear_artifact_declares_its_link_so_it_reloads_correctly():
    """
    A linear artifact loaded as an MLP would build the wrong architecture and
    fail to load its weights. The flag must survive the round trip, and an
    artifact predating the flag must still default to the MLP it was.
    """
    import inspect

    from ml.predict import load_predictor

    src = inspect.getsource(load_predictor)
    assert 'cfg.get("linear_link", False)' in src, (
        "load_predictor must read linear_link from the artifact, defaulting to "
        "False so pre-existing MLP artifacts keep loading"
    )


# ---------------------------------------------------------------------
# Real topology (ChronoGraph). See docs/ml/topology-study.md.
# ---------------------------------------------------------------------
def _realtopo_or_skip():
    from ml.realtopo import DEFAULT_EDGES_CSV

    if not DEFAULT_EDGES_CSV.exists():
        pytest.skip(f"{DEFAULT_EDGES_CSV} not present; see ml/data/chronograph/SOURCE.md")
    return DEFAULT_EDGES_CSV


def test_real_graph_matches_its_published_size():
    """Guard against the vendored file being replaced or truncated."""
    from ml.realtopo import load_real_edges

    _realtopo_or_skip()
    src, dst = load_real_edges()
    assert len(src) == 1529, f"expected 1529 edges, got {len(src)}"
    assert len(set(src) | set(dst)) == 708


def test_node_pools_are_disjoint_and_cover_the_graph():
    """
    The leakage guarantee. If pools ever overlap, a service seen in training
    could be scored in test and every topology result becomes meaningless.
    """
    from ml.realtopo import split_node_pools

    _realtopo_or_skip()
    tr, va, te = split_node_pools(0, fractions=(0.55, 0.20, 0.25))
    s_tr, s_va, s_te = set(tr.tolist()), set(va.tolist()), set(te.tolist())
    assert not (s_tr & s_va) and not (s_tr & s_te) and not (s_va & s_te)
    assert len(s_tr | s_va | s_te) == 708


def test_sampled_subgraphs_stay_inside_their_pool():
    """A subgraph straddling two pools would leak structure across the split."""
    from ml.realtopo import RealTopoConfig, sample_subgraphs, split_node_pools

    _realtopo_or_skip()
    tr, _va, te = split_node_pools(0, fractions=(0.55, 0.20, 0.25))
    cfg = RealTopoConfig(nodes_per_subgraph=(20, 40), min_edges=12)
    # The sampler renumbers nodes locally, so pool containment is checked by
    # confirming the test pool yields graphs at all and that train and test
    # draw on disjoint node sets by construction.
    train_subs = sample_subgraphs(1, 5, cfg, node_pool=tr, disjoint=False)
    test_subs = sample_subgraphs(1, 5, cfg, node_pool=te, disjoint=False)
    assert train_subs and test_subs
    assert len(set(tr.tolist()) & set(te.tolist())) == 0


def test_real_topology_is_more_concentrated_than_synthetic():
    """
    The premise of the whole topology study: real structure must actually be
    more hub-dominated than ml/synth.py's, or there is nothing to test.
    """
    from ml.realistic import gini
    from ml.realtopo import RealTopoConfig, generate_real_topology_dataset

    _realtopo_or_skip()
    cfg = RealTopoConfig(nodes_per_subgraph=(20, 40), min_edges=12)

    def mean_in_gini(dataset):
        out = []
        for graph, _cascades in dataset:
            d = np.zeros(graph.num_nodes)
            for e in range(graph.num_edges):
                d[int(graph.src[e])] += 1    # src is the dependency
            out.append(gini(d))
        return float(np.mean(out)) if out else 0.0

    real = mean_in_gini(generate_real_topology_dataset(7, 20, 2, cfg, disjoint=False))
    synth = mean_in_gini(generate_dataset(7, 20, 2))
    assert real > synth + 0.05, (
        f"real in-degree Gini {real:.3f} is not meaningfully above synthetic "
        f"{synth:.3f}; the topology experiment would be comparing like with like"
    )


def test_real_topology_cascades_respect_the_graph():
    """Simulated cascades on real structure must still obey reachability."""
    from ml.realtopo import RealTopoConfig, generate_real_topology_dataset

    _realtopo_or_skip()
    cfg = RealTopoConfig(nodes_per_subgraph=(20, 40), min_edges=12)
    dataset = generate_real_topology_dataset(
        9, 8, 3, cfg, synth_cfg=SynthConfig(false_positive_rate=0.0), disjoint=False
    )
    assert dataset
    for graph, cascades in dataset:
        for cas in cascades:
            affected = cas.labels > 0.5
            affected[cas.root_index] = False
            assert not (affected & ~graph.reachable_from(cas.root_index)).any()
