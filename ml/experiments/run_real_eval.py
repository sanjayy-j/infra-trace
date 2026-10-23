"""
Transfer check: train on synthetic estates, evaluate on the 22 real incidents.

READ THE SAMPLE SIZE FIRST
--------------------------
The real evaluation set is 22 incidents covering 66 decisions. That is very
small. A difference of one or two components moves every metric visibly, and
no result here should be read as establishing that one method is better than
another in general. Confidence intervals are bootstrapped OVER SCENARIOS
(resampling whole incidents, not individual components, because components
within one incident are not independent) and they are wide on purpose.

What this experiment can honestly establish:
  * whether a model trained only on generated graphs produces sensible
    numbers on a real topology it has never seen, or falls apart;
  * whether it reduces the false positives that reachability produces,
    on real recorded incidents rather than simulated ones.

What it cannot establish:
  * that the improvement generalises to other organisations' systems. The
    ShopSphere data in database/data/seed.sql was hand-authored for this
    project, so the propagation patterns in it are a plausible model of a
    real estate, not a sample from one.

Run:
    python -m ml.experiments.run_real_eval
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from ..evaluate import summarize
from ..extract import load_graph, load_incident_cascades, verify_against_sql
from ..graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM, Cascade, Graph
from ..models import (
    DepthDecayBaseline,
    GCNNet,
    LogisticBaseline,
    ReachabilityBaseline,
    SCPConfig,
    SCPNet,
)
from ..synth import generate_dataset
from ..train import (
    TrainConfig,
    collect_predictions,
    collect_predictions_classical,
    to_tensors,
)

RESULTS = Path(__file__).resolve().parent / "results"


def bootstrap_ci(
    per_scenario: list[tuple[np.ndarray, np.ndarray]],
    metric: str,
    n_boot: int = 2000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Percentile bootstrap over whole scenarios.

    Resamples incidents with replacement, pools their decisions, recomputes
    the metric. Resampling individual components instead would treat the
    four components of one incident as four independent observations and
    produce an interval that is far too narrow.
    """
    rng = np.random.default_rng(seed)
    point_y = np.concatenate([y for y, _ in per_scenario])
    point_p = np.concatenate([p for _, p in per_scenario])
    point = summarize(point_y, point_p)[metric]

    vals = []
    idx = np.arange(len(per_scenario))
    for _ in range(n_boot):
        pick = rng.choice(idx, size=idx.size, replace=True)
        ys = np.concatenate([per_scenario[i][0] for i in pick])
        ps = np.concatenate([per_scenario[i][1] for i in pick])
        v = summarize(ys, ps)[metric]
        if np.isfinite(v):
            vals.append(v)
    if not vals:
        return point, float("nan"), float("nan")
    return point, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def _per_scenario_torch(model, graph: Graph, cascades: list[Cascade], **kw):
    out = []
    t = to_tensors(graph)
    for cas in cascades:
        mask = cas.eval_mask(graph)
        if not mask.any():
            continue
        roots = torch.tensor([cas.root_index], dtype=torch.long)
        with torch.no_grad():
            q = model(t["x"], t["src"], t["dst"], t["edge_attr"], roots, **kw)[0]
        out.append((cas.labels[mask], q.numpy()[mask]))
    return out


def _per_scenario_classical(model, graph: Graph, cascades: list[Cascade]):
    out = []
    for cas in cascades:
        mask = cas.eval_mask(graph)
        if not mask.any():
            continue
        out.append((cas.labels[mask], model.predict(graph, cas.root_index)[mask]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=120)
    args = ap.parse_args()

    print("Loading the real graph from MySQL ...")
    graph = load_graph()
    print("  verifying Python traversal against fn_blast_radius_count ...")
    print(f"  {verify_against_sql(graph)}")
    cascades = load_incident_cascades(graph)
    decisions = sum(int(c.eval_mask(graph).sum()) for c in cascades)
    positives = sum(int(c.labels[c.eval_mask(graph)].sum()) for c in cascades)
    print(
        f"  real graph N={graph.num_nodes} E={graph.num_edges}; "
        f"{len(cascades)} incidents, {decisions} decisions, {positives} positive"
    )

    real_ds = [(graph, cascades)]
    rows: dict[str, dict] = {}

    # --- the system as it stands, plus shallow baselines ---
    reach_scen = _per_scenario_classical(ReachabilityBaseline(), graph, cascades)
    rows["Reachability (current SQL)"] = {"per_scenario": reach_scen}

    # --- models trained on synthetic data only ---
    train_ds = generate_dataset(11, 70, 6)
    val_ds = generate_dataset(12, 20, 6)

    decay = DepthDecayBaseline().fit(train_ds)
    rows["Depth decay"] = {"per_scenario": _per_scenario_classical(decay, graph, cascades)}
    logistic = LogisticBaseline().fit(train_ds)
    rows["Logistic (path feats)"] = {
        "per_scenario": _per_scenario_classical(logistic, graph, cascades)
    }

    from ..train import train_model

    for name, ctor in (
        ("GCN", lambda: GCNNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        (
            "SCP-GNN (proposed)",
            lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        ),
    ):
        # Average predictions over seeds: a single synthetic training run is
        # a noisy draw, and reporting one of them would be cherry-picking.
        stacked: list[list[np.ndarray]] = []
        for seed in range(args.seeds):
            torch.manual_seed(seed)
            model = ctor()
            train_model(model, train_ds, val_ds, TrainConfig(epochs=args.epochs, seed=seed))
            scen = _per_scenario_torch(model, graph, cascades)
            stacked.append([p for _, p in scen])
        ys = [y for y, _ in _per_scenario_torch(model, graph, cascades)]
        averaged = [
            (ys[i], np.mean([s[i] for s in stacked], axis=0)) for i in range(len(ys))
        ]
        rows[name] = {"per_scenario": averaged}

    # --- report ---
    print()
    print(f"Real incidents (n={len(cascades)} scenarios, {decisions} decisions)")
    print("95% bootstrap CI over scenarios, 2000 resamples")
    print()
    header = (
        f"{'model':30}{'PR-AUC (95% CI)':>26}{'prec@.5':>9}{'rec@.5':>8}"
        f"{'F1@.5':>8}{'bestF1':>8}{'@thr':>7}"
    )
    print(header)
    print("-" * len(header))

    out_json: dict[str, dict] = {}
    for name, payload in rows.items():
        scen = payload["per_scenario"]
        y = np.concatenate([a for a, _ in scen])
        p = np.concatenate([b for _, b in scen])
        m = summarize(y, p)
        point, lo, hi = bootstrap_ci(scen, "pr_auc")
        ci = f"{point:.3f} [{lo:.3f}, {hi:.3f}]" if np.isfinite(lo) else f"{point:.3f}"
        print(
            f"{name:30}{ci:>26}{m['precision']:>9.3f}{m['recall']:>8.3f}"
            f"{m['f1']:>8.3f}{m['best_f1']:>8.3f}{m['best_threshold']:>7.2f}"
        )
        out_json[name] = {
            **{k: v for k, v in m.items() if isinstance(v, (int, float))},
            "pr_auc_ci_low": lo,
            "pr_auc_ci_high": hi,
        }

    print()
    print(f"n={len(cascades)} incidents. The intervals overlap heavily; this is a")
    print("sanity check that synthetic training transfers, not evidence of a")
    print("general improvement. See the module docstring.")
    print()
    print("F1@.5 vs bestF1 is the gap that matters for deployment: the models")
    print("are trained on synthetic data whose positive rate (~26%) differs from")
    print("the real one (44%), so 0.5 is not the right operating point here and")
    print("the threshold has to be chosen on held-out data, not assumed.")

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "real_eval.json"
    path.write_text(json.dumps(out_json, indent=2), encoding="utf-8")
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
