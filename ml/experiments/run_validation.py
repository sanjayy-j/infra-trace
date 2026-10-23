"""
Pre-flight validation for the corrected topology protocol.

Checks the EVALUATION SUBSTRATE before any full experiment is authorised. It
exercises the real code paths the full study will use - partitioning
(ml/partition.py), subgraph sampling, feature construction (ml/graph.from_edges),
cascade simulation (ml/synth.simulate_cascade) and the metric-output path -
on a deliberately tiny configuration.

MODEL SCORES ARE DISCARDED
--------------------------
A minimal training run happens only to prove the metric path produces
reproducible output. Its PR-AUC values are written to a validation-scoped file,
never reported as results, and never compared between configurations. Nothing
here can be used to select a model, a hyperparameter or a threshold - and no
model artifact is written, so ml/artifacts/ is untouched.

Run:
    python -m ml.experiments.run_validation
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import torch

from ..evaluate import summarize
from ..graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM
from ..models import LogisticBaseline, SCPConfig, SCPNet
from ..partition import (
    DEFAULT_FRACTIONS,
    MAX_POOL_DEVIATION,
    POOLS,
    SampleConfig,
    build_graph,
    largest_remainder,
    sample_pool_subgraphs,
    stratified_split_node_pools,
)
from ..realtopo import _truth_for
from ..synth import SynthConfig, simulate_cascade
from ..train import TrainConfig, collect_predictions, train_model

RESULTS = Path(__file__).resolve().parent / "results"
PROTECTED = [
    "ml/models/scp.py", "ml/models/scp_bid.py", "ml/models/scp_latent.py",
    "ml/models/baselines.py", "ml/models/__init__.py", "ml/synth.py", "ml/graph.py",
    "ml/train.py", "ml/predict.py", "ml/train_production.py", "ml/realtopo.py",
    "backend/app/ml_api.py", "backend/app/main.py",
    "ml/artifacts/scp_gnn.pt", "ml/artifacts/scp_linear.pt",
]
ROOT = Path(__file__).resolve().parents[2]


def hash_protected() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in PROTECTED:
        p = ROOT / rel
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "MISSING"
    return out


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:46} {detail}")

    @property
    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.rows if not ok]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partitions", type=int, default=2)
    ap.add_argument("--inits", type=int, default=1)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--scan", type=int, default=8)
    args = ap.parse_args()

    before = hash_protected()
    checks = Checks()
    cfg = SampleConfig(nodes_per_subgraph=(40, 120), min_edges=25)

    # ---------------------------------------------------------------
    print("\n=== Quota allocation (largest remainder) ===")
    for size, expect in ((11, (6, 2, 3)), (19, (9, 4, 6)), (79, (39, 16, 24))):
        got = largest_remainder(size, DEFAULT_FRACTIONS)
        checks.record(f"quota tier size {size} sums exactly",
                      sum(got) == size and got == expect, f"{got}")
    edge = all(sum(largest_remainder(t, DEFAULT_FRACTIONS)) == t for t in range(0, 200))
    checks.record("quota conserves total for sizes 0..199", edge)

    # ---------------------------------------------------------------
    print("\n=== Partition feasibility scan ===")
    feasible, rejected = [], []
    for p in range(args.scan):
        part = stratified_split_node_pools(p)
        (feasible if part.feasible else rejected).append(part if part.feasible else p)
        sizes = {k: len(v) for k, v in part.pools.items()}
        dev = max(part.deviation.values())
        print(f"    seed {p}: sizes {sizes}  max deviation {dev:.3f}  "
              f"{'OK' if part.feasible else 'REJECTED'}")
    checks.record("at least 2 feasible partitions in scan", len(feasible) >= 2,
                  f"{len(feasible)} feasible, rejected seeds {rejected}")
    checks.record("rejection rate below 40%", len(rejected) / max(args.scan, 1) < 0.4,
                  f"{len(rejected)}/{args.scan}")
    if len(feasible) < 2:
        print("\nHALT: fewer than 2 feasible partitions.")
        raise SystemExit(1)

    parts = feasible[: args.partitions]

    # ---------------------------------------------------------------
    print("\n=== Partition invariants ===")
    for part in parts:
        sets = [set(part.pools[p].tolist()) for p in POOLS]
        disjoint = all(not (sets[i] & sets[j]) for i in range(3) for j in range(i + 1, 3))
        checks.record(f"V1 seed {part.seed}: pools disjoint, cover 708",
                      disjoint and len(set().union(*sets)) == 708)
        same = stratified_split_node_pools(part.seed)
        checks.record(f"V2 seed {part.seed}: deterministic",
                      all(np.array_equal(same.pools[p], part.pools[p]) for p in POOLS))
        seen: dict[int, str] = {}
        dup = False
        for h in part.hubs:
            if h.node in seen and seen[h.node] != h.pool:
                dup = True
            seen[h.node] = h.pool
        checks.record(f"V3 seed {part.seed}: no hub in two pools", not dup)
        checks.record(f"V5 seed {part.seed}: edge accounting complete",
                      all(h.retained_in_degree + h.cut_edges == h.global_in_degree
                          for h in part.hubs))
        checks.record(f"V7 seed {part.seed}: deviation <= {MAX_POOL_DEVIATION}",
                      max(part.deviation.values()) <= MAX_POOL_DEVIATION,
                      f"max {max(part.deviation.values()):.3f}")
        t1 = {p: len(part.hubs_in(p, "T1")) for p in POOLS}
        t2 = {p: len(part.hubs_in(p, "T2")) for p in POOLS}
        checks.record(f"V8 seed {part.seed}: every pool has T1>=1 and T2>=2",
                      all(t1[p] >= 1 for p in POOLS) and all(t2[p] >= 2 for p in POOLS),
                      f"T1 {t1} T2 {t2}")

    # ---------------------------------------------------------------
    print("\n=== Hub table: presence vs preservation (test pool, seed "
          f"{parts[0].seed}) ===")
    print(f"    {'node':>6}{'tier':>6}{'global':>8}{'retained':>10}{'retain%':>9}"
          f"{'cut':>6}{'cap@120':>9}")
    for h in sorted(parts[0].hubs_in("test"), key=lambda x: -x.global_in_degree)[:10]:
        print(f"    {h.node:>6}{h.tier:>6}{h.global_in_degree:>8}"
              f"{h.retained_in_degree:>10}{h.retention_fraction * 100:>8.0f}%"
              f"{h.cut_edges:>6}{h.observability_cap(120):>9}")
    checks.record("V11 observability cap <= subgraph size - 1",
                  all(h.observability_cap(120) <= 119 for h in parts[0].hubs))

    # ---------------------------------------------------------------
    print("\n=== Evaluation-set separation (TEST-R primary, TEST-H secondary) ===")
    part = parts[0]
    hubs = [h.node for h in part.hubs_in("test")]
    test_r = sample_pool_subgraphs(part.pools["test"], 101, 6, cfg, hub_seeded=False)
    test_h = sample_pool_subgraphs(part.pools["test"], 101, 6, cfg,
                                   hub_seeded=True, hub_nodes=hubs)
    checks.record("V10a TEST-R produced", len(test_r) > 0, f"{len(test_r)} subgraphs")
    checks.record("V10b TEST-H produced", len(test_h) > 0, f"{len(test_h)} subgraphs")
    checks.record("V10c TEST-H subgraphs each contain an assigned hub",
                  all(k & set(hubs) for _c, _d, k in test_h))
    checks.record("V10d sets are distinct",
                  [frozenset(k) for *_, k in test_r] != [frozenset(k) for *_, k in test_h])
    if test_r:
        cov_r = len(set().union(*[k for *_, k in test_r]))
        cov_h = len(set().union(*[k for *_, k in test_h])) if test_h else 0
        print(f"    TEST-R covers {cov_r} services; TEST-H covers {cov_h}; "
              f"test pool has {len(part.pools['test'])}")

    # ---------------------------------------------------------------
    print("\n=== Cascade legality through the real simulation path ===")
    scfg = SynthConfig(false_positive_rate=0.0)
    rng = np.random.default_rng(0)
    legal = True
    for comps, deps, _k in test_r[:3]:
        g = build_graph(comps, deps)
        truth = _truth_for(g, deps, rng, scfg)
        for _ in range(3):
            root = int(rng.integers(0, g.num_nodes))
            cas = simulate_cascade(rng, g, truth, root, scfg)
            aff = cas.labels > 0.5
            aff[root] = False
            if (aff & ~g.reachable_from(root)).any():
                legal = False
    checks.record("V9 every affected node reachable from its root", legal)

    # ---------------------------------------------------------------
    print("\n=== Metric-output path (tiny run; SCORES DISCARDED) ===")
    def make_split(part, seed, n, hub_seeded=False):
        hubs_ = [h.node for h in part.hubs_in("test")] if hub_seeded else None
        out = []
        r = np.random.default_rng(seed)
        for comps, deps, _k in sample_pool_subgraphs(
            part.pools["train" if not hub_seeded else "test"], seed, n, cfg,
            hub_seeded=hub_seeded, hub_nodes=hubs_
        ):
            g = build_graph(comps, deps)
            truth = _truth_for(g, deps, r, SynthConfig())
            reach = np.array([g.reachable_from(i).sum() for i in range(g.num_nodes)],
                             dtype=float)
            if reach.sum() == 0:
                continue
            w = reach / reach.sum()
            cas = [simulate_cascade(r, g, truth, int(r.choice(g.num_nodes, p=w)),
                                    SynthConfig()) for _ in range(3)]
            out.append((g, cas))
        return out

    metrics: dict[str, dict] = {}
    for part in parts:
        tr = make_split(part, part.seed * 10 + 1, 6)
        va = make_split(part, part.seed * 10 + 2, 3)
        if not tr or not va:
            checks.record(f"metric path seed {part.seed}", False, "empty split")
            continue
        torch.manual_seed(0)
        model = SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4))
        train_model(model, tr, va, TrainConfig(epochs=args.epochs, seed=0))
        y, p = collect_predictions(model, va)
        res = summarize(y, p)
        metrics[f"seed{part.seed}"] = {
            k: v for k, v in res.items() if isinstance(v, (int, float))
        }
        checks.record(f"metric path seed {part.seed} emits finite PR-AUC",
                      np.isfinite(res["pr_auc"]))
        lg = LogisticBaseline().fit(tr)
        checks.record(f"logistic baseline fits on seed {part.seed}", lg._fitted)

    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / "validation_metrics_DISCARD.json"
    out_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")

    # reproducibility of the metric file
    first = out_path.read_text(encoding="utf-8")
    out_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    checks.record("V15 metric output reproducible",
                  first == out_path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------
    print("\n=== V16 protected files unchanged ===")
    after = hash_protected()
    changed = [k for k in before if before[k] != after[k]]
    checks.record("V16 no protected file or artifact modified", not changed,
                  f"changed: {changed}" if changed else "all 15 identical")

    print("\n" + "=" * 70)
    if checks.failed:
        print(f"VALIDATION FAILED: {len(checks.failed)} check(s)")
        for n in checks.failed:
            print(f"  - {n}")
        raise SystemExit(1)
    print(f"VALIDATION PASSED: {len(checks.rows)} checks")
    print("Model scores were discarded and are not reported.")
    print("=" * 70)


if __name__ == "__main__":
    main()
