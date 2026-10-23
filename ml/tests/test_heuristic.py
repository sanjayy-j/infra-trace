"""
Tests for the fixed-weight propagation heuristic (ml/heuristic.py).

These check the MECHANISM, not model quality. No assertion here involves a
score on test data, so nothing in this file can be used to select an arm.

The load-bearing checks are:
  * the weight table is addressed correctly (a Synchronous critical edge gets
    the Synchronous/critical cell, not a neighbouring one);
  * the noisy-OR arm reproduces hand-computed probabilities;
  * noisy-OR and max-product genuinely differ where they should (parallel
    paths) and agree where they must (a single chain);
  * the safety property SCP-GNN has - an unreachable component is never
    predicted to fail - holds here too, so the two models are judged on the
    same candidate set for the same reason.
"""

from __future__ import annotations

import numpy as np
import torch

from ml.graph import from_edges
from ml.heuristic import (
    CELL_NAMES,
    IR_MULTIPLIERS,
    NUM_CELLS,
    ORACLE_WEIGHTS,
    FixedWeightPropagation,
    HeuristicConfig,
    arms,
    cell_selector,
)
from ml.train import to_tensors


def _graph(edges, n=4, dep_type="Synchronous", crit=1):
    """Tiny graph from dependency rows. `edges` are (dependent, dependency)."""
    comps = [
        {"component_id": i, "component_name": f"c{i}", "component_type": "Service",
         "criticality": "Medium", "is_active": 1}
        for i in range(n)
    ]
    deps = [
        {"component_id": u, "depends_on_id": v,
         "dependency_type": dep_type, "is_critical": crit}
        for u, v in edges
    ]
    return from_edges(comps, deps)


def _run(model, graph, root):
    t = to_tensors(graph)
    roots = torch.tensor([root], dtype=torch.long)
    model.eval()
    with torch.no_grad():
        return model(t["x"], t["src"], t["dst"], t["edge_attr"], roots)[0].numpy()


def _fixed(weights, aggregator="noisy_or", rounds=4):
    return FixedWeightPropagation(
        HeuristicConfig(name="t", weights=weights, aggregator=aggregator, rounds=rounds)
    )


# ---------------------------------------------------------------------
# Table addressing
# ---------------------------------------------------------------------
def test_cell_names_are_the_expected_eight_plus_fallback():
    assert len(CELL_NAMES) == NUM_CELLS == 9
    assert CELL_NAMES[0] == "Synchronous/critical"
    assert CELL_NAMES[1] == "Synchronous/non-critical"
    assert CELL_NAMES[-1] == "unknown-type"


def test_selector_is_one_hot_and_hits_the_right_cell():
    """Off-by-one here would silently score every edge with a wrong weight."""
    for dep_type in ("Synchronous", "Asynchronous", "Data", "Config"):
        for crit in (0, 1):
            g = _graph([(1, 0)], n=2, dep_type=dep_type, crit=crit)
            sel = cell_selector(torch.as_tensor(g.edge_features))
            assert sel.shape == (1, NUM_CELLS)
            assert sel.sum().item() == 1.0
            name = f"{dep_type}/{'critical' if crit else 'non-critical'}"
            assert CELL_NAMES[int(sel.argmax())] == name


def test_unknown_dependency_type_routes_to_the_fallback_cell():
    g = _graph([(1, 0)], n=2, dep_type="Telepathy", crit=1)
    sel = cell_selector(torch.as_tensor(g.edge_features))
    assert CELL_NAMES[int(sel.argmax())] == "unknown-type"


def test_edge_probability_equals_the_tabled_weight():
    g = _graph([(1, 0)], n=2, dep_type="Asynchronous", crit=0)
    m = _fixed(ORACLE_WEIGHTS)
    p = m.edge_probabilities(torch.as_tensor(g.edge_features))
    assert abs(float(p[0]) - ORACLE_WEIGHTS["Asynchronous/non-critical"]) < 1e-6


def test_weight_table_round_trips():
    """Weights are stored as float32 like every other model tensor, so the
    comparison is to float32 precision, not exact equality."""
    m = _fixed(IR_MULTIPLIERS)
    got = m.weight_table()
    assert set(got) == set(IR_MULTIPLIERS)
    assert all(abs(got[k] - IR_MULTIPLIERS[k]) < 1e-6 for k in IR_MULTIPLIERS)


# ---------------------------------------------------------------------
# Propagation arithmetic, against values computed by hand
# ---------------------------------------------------------------------
def test_chain_is_the_product_of_edge_probabilities():
    """0 -> 1 -> 2 at p = 0.8 each: P(2) = 0.64 exactly."""
    w = {k: 0.8 for k in CELL_NAMES}
    g = _graph([(1, 0), (2, 1)], n=3)
    q = _run(_fixed(w), g, root=0)
    assert abs(q[1] - 0.8) < 1e-5
    assert abs(q[2] - 0.64) < 1e-5


def test_parallel_paths_separate_the_two_combination_rules():
    """Diamond 0->{1,2}->3, all p = 0.5.

    noisy-OR     1 - (1 - 0.25)^2 = 0.4375   two weak paths reinforce
    max-product  max(0.25, 0.25)  = 0.25     the strongest path wins

    This is the substantive difference between SCP-GNN's rule and
    IncidentRelay's, and it is worth 0.1875 of probability here.
    """
    w = {k: 0.5 for k in CELL_NAMES}
    g = _graph([(1, 0), (2, 0), (3, 1), (3, 2)], n=4)
    q_or = _run(_fixed(w, "noisy_or"), g, root=0)
    q_mp = _run(_fixed(w, "max_product"), g, root=0)
    assert abs(q_or[3] - 0.4375) < 1e-5
    assert abs(q_mp[3] - 0.25) < 1e-5
    assert q_or[3] > q_mp[3]


def test_the_two_rules_agree_on_a_single_chain():
    """With one path there is nothing to combine, so the rules must coincide."""
    w = {k: 0.7 for k in CELL_NAMES}
    g = _graph([(1, 0), (2, 1)], n=3)
    q_or = _run(_fixed(w, "noisy_or"), g, root=0)
    q_mp = _run(_fixed(w, "max_product"), g, root=0)
    assert np.allclose(q_or, q_mp, atol=1e-5)


# ---------------------------------------------------------------------
# Safety and invariants - the same ones SCP-GNN is held to
# ---------------------------------------------------------------------
def test_unreachable_components_are_never_predicted_to_fail():
    """0 -> 1; node 2 and 3 are isolated. Both combination rules, every arm.

    SCP-GNN's output on an unreachable component is exactly the EPS floor,
    and the evaluation mask drops those entries. If this model scored them
    above the floor it would be answering a different question from SCP-GNN
    on the same mask.
    """
    g = _graph([(1, 0), (3, 2)], n=4)
    reach = g.reachable_from(0)
    assert not reach[2] and not reach[3]
    for cfg in arms():
        if cfg.trainable:
            model = FixedWeightPropagation(cfg)
        else:
            model = FixedWeightPropagation(cfg)
        q = _run(model, g, root=0)
        assert q[2] <= 1e-6 + 1e-9
        assert q[3] <= 1e-6 + 1e-9


def test_root_is_one_and_is_not_a_prediction():
    g = _graph([(1, 0), (2, 1)], n=3)
    for rule in ("noisy_or", "max_product"):
        q = _run(_fixed({k: 0.5 for k in CELL_NAMES}, rule), g, root=0)
        assert abs(q[0] - 1.0) < 1e-9


def test_probabilities_stay_in_range_on_a_dense_cyclic_graph():
    """Cycles must not drive the fixed point out of [0, 1] or oscillate."""
    edges = [(i, j) for i in range(6) for j in range(6) if i != j]
    g = _graph(edges, n=6)
    for rule in ("noisy_or", "max_product"):
        q = _run(_fixed({k: 0.9 for k in CELL_NAMES}, rule, rounds=12), g, root=0)
        assert np.all(q >= 0.0) and np.all(q <= 1.0)


def test_raising_a_weight_cannot_lower_a_prediction():
    """Monotone in the weights - a stronger dependency cannot mean less risk."""
    g = _graph([(1, 0), (2, 1), (2, 0)], n=3)
    low = _run(_fixed({k: 0.3 for k in CELL_NAMES}), g, root=0)
    high = _run(_fixed({k: 0.6 for k in CELL_NAMES}), g, root=0)
    assert np.all(high + 1e-6 >= low)


def test_node_features_are_ignored_by_construction():
    """The information asymmetry is enforced, not just documented.

    Perturbing every node feature must leave the output bitwise identical.
    If this ever fails, the heuristic has gained access to information the
    incumbent it represents does not have.
    """
    g = _graph([(1, 0), (2, 1)], n=3)
    m = _fixed(ORACLE_WEIGHTS)
    before = _run(m, g, root=0)
    g.node_features = (g.node_features + 0.37).astype(np.float32)
    after = _run(m, g, root=0)
    assert np.array_equal(before, after)


# ---------------------------------------------------------------------
# The fitted arm
# ---------------------------------------------------------------------
def test_fitted_arm_has_exactly_nine_parameters():
    """Eight cells plus the unknown-type fallback, against SCP-GNN's 1,441."""
    m = FixedWeightPropagation([c for c in arms() if c.trainable][0])
    assert sum(p.numel() for p in m.parameters()) == NUM_CELLS


def test_fitted_arm_starts_at_one_half_and_is_differentiable():
    cfg = [c for c in arms() if c.trainable][0]
    m = FixedWeightPropagation(cfg)
    assert all(abs(v - 0.5) < 1e-6 for v in m.weight_table().values())
    g = _graph([(1, 0), (2, 1)], n=3)
    t = to_tensors(g)
    out = m(t["x"], t["src"], t["dst"], t["edge_attr"], torch.tensor([0]))
    out.sum().backward()
    assert m.logit.grad is not None
    assert torch.isfinite(m.logit.grad).all()


def test_a_fixed_arm_has_no_trainable_parameters():
    """A fixed arm must not drift if it is ever handed to an optimiser."""
    m = _fixed(IR_MULTIPLIERS)
    assert sum(p.numel() for p in m.parameters()) == 0


def test_arms_are_the_four_pre_registered_ones():
    names = [c.name for c in arms()]
    assert names == [
        "H-IR-noisyOR", "H-IR-maxProduct", "H-ORACLE", "H-FIT (8 params)"
    ]
    assert sum(c.trainable for c in arms()) == 1
