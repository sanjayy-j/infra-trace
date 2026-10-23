"""
Fixed-weight propagation heuristic vs SCP-GNN, on the frozen TEST-R partitions.

WHY
---
`final-review/07-NOVELTY-AUDIT.md` found that the obvious incumbent baseline -
a hand-authored dependency multiplier table of the kind IncidentRelay
documents - had never been run. Until it is, "a learned propagation model
beats hand-set edge weights" is an unsupported claim. This script runs it.

HOW THE COMPARISON IS MADE FAIR
-------------------------------
Nothing is retrained and no frozen artifact is touched. SCP-GNN's numbers are
read from `results/topology_v2.json` exactly as stored. The test data is
REGENERATED, not re-sampled: `evaluate_partition` in run_topology_v2.py
consumes one `default_rng(part.seed)` stream in a fixed order - train, val,
TEST-R, TEST-H - entirely before any torch seeding, so replaying that order
reproduces the same subgraphs, the same cascades and the same labels.

That claim is not assumed. Every partition recomputes the three deterministic
baselines from the frozen run (reachability, depth decay, logistic) and
asserts they match the stored values to 1e-12. If the data had drifted by one
RNG draw, those three numbers would move and the run would halt. The check is
in the output artifact under "reproduction".

The heuristic then scores through `ml.train.collect_predictions` and
`ml.evaluate.summarize` - the same mask, the same threshold, the same ECE
binning, the same code - as SCP-GNN did.

THE FOUR ARMS (frozen before any result was seen; see ml/heuristic.py)
----------------------------------------------------------------------
  H-IR-noisyOR      IncidentRelay's documented multipliers, SCP's noisy-OR.
  H-IR-maxProduct   IncidentRelay's multipliers AND its documented rule
                    (multiply the upstream score). The deployable incumbent.
  H-ORACLE          handed the generator's own per-type rates. NOT deployable;
                    the upper bound for any fixed (type x criticality) table.
  H-FIT (8 params)  9 learnable cells, trained through the identical loop,
                    loss, epoch budget and early stopping as SCP-GNN, on
                    train/val only. The information-matched arm.

INFORMATION ASYMMETRY
---------------------
Every arm sees only edge type, criticality and topology. SCP-GNN also sees
component type, criticality, is_active and both degrees, and can condition an
edge on its endpoints. So a win for SCP-GNN over H-FIT measures the value of
node context plus the MLP; a win over H-ORACLE measures that it beats a table
that was given the right numbers. Those are different claims and are reported
separately.

PRE-REGISTERED DECISION RULES
-----------------------------
Primary metric: paired per-partition PR-AUC on TEST-R, SCP-GNN minus arm,
over the same partitions the frozen study analysed.

  R1  "Learned propagation beats hand-set weights" is supported only if
      SCP-GNN's mean PR-AUC gap over BOTH H-IR arms is positive AND positive
      in >= 26/30 partitions - the same consistency bar the frozen study used.
  R2  If SCP-GNN does not beat H-FIT by that bar, the honest conclusion is
      that a 9-parameter interpretable table matches the 1,441-parameter MLP,
      which WEAKENS the case for the MLP and must be reported as such.
  R3  H-ORACLE beating SCP-GNN establishes nothing about deployment: it only
      shows that knowing the generator's parameters helps. It is reported as
      a diagnostic, never as a baseline SCP-GNN "lost" to.
  R4  No significance test is a decision criterion. Partitions share
      services (claim C2/C3 in final-review/00-CLAIMS-STATUS.md), so no
      interval here has valid coverage. Spread is reported descriptively.
  R5  ECE is reported for every arm and is not a gate. A fixed table with
      multipliers near 1.0 is expected to be badly calibrated; that is
      information about the heuristic, not a tuning target.

WHAT THIS CANNOT ESTABLISH
--------------------------
Nothing about novelty: both halves of SCP-GNN are prior art (Kalimeris 2018,
Xiang 2024) and so is the heuristic (IncidentRelay). Nothing about
independent-system generalisation: one 708-service graph, re-partitioned.
Nothing about real failures: the cascades are simulated.

Run:
    python -m ml.experiments.run_fixed_weight --partitions 32
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch

from ..evaluate import summarize
from ..heuristic import FixedWeightPropagation, arms
from ..models import DepthDecayBaseline, LogisticBaseline, ReachabilityBaseline
from ..partition import POOLS, stratified_split_node_pools
from ..train import (
    TrainConfig,
    collect_predictions,
    collect_predictions_classical,
    train_model,
)
from .run_topology_v2 import (
    CASCADES,
    CFG,
    EPOCHS,
    N_TEST,
    N_TRAIN,
    N_VAL,
    build_dataset,
    hash_protected,
)

RESULTS = Path(__file__).resolve().parent / "results"
FROZEN = RESULTS / "topology_v2.json"
OUT = RESULTS / "fixed_weight_baseline.json"

METRICS = ("pr_auc", "precision", "recall", "f1", "ece")
REPRO_TOL = 1e-12

# The three deterministic models whose frozen values prove the regenerated
# data is the same data. Reachability is parameter-free; the other two are
# fitted on the regenerated TRAIN pool, so they also prove the train split
# reproduced.
REPRO_MODELS = (
    ("Reachability (SQL)", lambda train_ds: ReachabilityBaseline()),
    ("Depth decay", lambda train_ds: DepthDecayBaseline().fit(train_ds)),
    ("Logistic (path feats)", lambda train_ds: LogisticBaseline().fit(train_ds)),
)


def _mean(values: list[float]) -> float:
    vals = [v for v in values if np.isfinite(v)]
    return float(np.mean(vals)) if vals else float("nan")


def evaluate_partition(part, frozen_rec: dict, inits: int) -> dict:
    """Regenerate one partition's data, verify it, then score every arm."""
    rng = np.random.default_rng(part.seed)
    hubs = [h.node for h in part.hubs_in("test")]

    train_ds, _ = build_dataset(part.pools["train"], part.seed * 97 + 1, N_TRAIN, rng)
    val_ds, _ = build_dataset(part.pools["val"], part.seed * 97 + 2, N_VAL, rng)
    test_r, _ = build_dataset(part.pools["test"], part.seed * 97 + 3, N_TEST, rng)
    test_h, _ = build_dataset(
        part.pools["test"], part.seed * 97 + 4, N_TEST, rng,
        hub_seeded=True, hub_nodes=hubs,
    )
    usable = bool(train_ds and val_ds and test_r)
    if usable != bool(frozen_rec.get("usable")):
        raise SystemExit(
            f"HALT seed {part.seed}: usability disagrees with the frozen run "
            f"(regenerated={usable}, frozen={frozen_rec.get('usable')}). "
            "The data did not reproduce; refusing to compare."
        )
    if not usable:
        return {"seed": part.seed, "usable": False}

    # --- reproduction check: must match the frozen artifact exactly ---
    repro = {}
    for name, ctor in REPRO_MODELS:
        model = ctor(train_ds)
        y, p = collect_predictions_classical(model, test_r)
        mine = summarize(y, p)
        stored = frozen_rec["metrics"][name]["TEST-R"]
        worst = max(abs(mine[k] - stored[k]) for k in METRICS)
        repro[name] = {"max_abs_delta": worst, "n": mine["n"]}
        if worst > REPRO_TOL:
            raise SystemExit(
                f"HALT seed {part.seed}: '{name}' TEST-R metrics differ from the "
                f"frozen run by {worst:.3e} (> {REPRO_TOL:.0e}). The regenerated "
                "data is not the frozen data; refusing to compare."
            )

    # --- the arms ---
    results: dict[str, dict] = {}
    tables: dict[str, dict] = {}
    for cfg in arms():
        per_set: dict[str, list[dict]] = {"TEST-R": [], "TEST-H": []}
        if cfg.trainable:
            # Identical protocol to the frozen neural runs: `inits` fits,
            # same torch seeding scheme, averaged WITHIN the partition.
            fitted_tables = []
            for init in range(inits):
                torch.manual_seed(part.seed * 1000 + init)
                model = FixedWeightPropagation(cfg)
                train_model(
                    model, train_ds, val_ds, TrainConfig(epochs=EPOCHS, seed=init)
                )
                fitted_tables.append(model.weight_table())
                for setname, ds in (("TEST-R", test_r), ("TEST-H", test_h)):
                    if ds:
                        y, p = collect_predictions(model, ds)
                        per_set[setname].append(summarize(y, p))
            tables[cfg.name] = {
                cell: _mean([t[cell] for t in fitted_tables])
                for cell in fitted_tables[0]
            }
        else:
            model = FixedWeightPropagation(cfg)
            tables[cfg.name] = model.weight_table()
            for setname, ds in (("TEST-R", test_r), ("TEST-H", test_h)):
                if ds:
                    y, p = collect_predictions(model, ds)
                    per_set[setname].append(summarize(y, p))

        results[cfg.name] = {
            setname: {k: _mean([m[k] for m in lst]) for k in METRICS}
            for setname, lst in per_set.items()
            if lst
        }

    return {
        "seed": part.seed,
        "usable": True,
        "pool_sizes": {p: int(len(part.pools[p])) for p in POOLS},
        "reproduction": repro,
        "weight_tables": tables,
        "metrics": results,
        "scp_gnn_frozen": {
            s: {k: frozen_rec["metrics"]["SCP-GNN"][s][k] for k in METRICS}
            for s in frozen_rec["metrics"]["SCP-GNN"]
        },
    }


def paired(records: list[dict], arm: str, setname: str, metric: str) -> dict:
    """Paired per-partition difference, SCP-GNN minus arm. Descriptive only."""
    diffs, scp, heur = [], [], []
    for r in records:
        if not r.get("usable"):
            continue
        a = r["metrics"].get(arm, {}).get(setname)
        s = r["scp_gnn_frozen"].get(setname)
        if not a or not s or not np.isfinite(a[metric]) or not np.isfinite(s[metric]):
            continue
        diffs.append(s[metric] - a[metric])
        scp.append(s[metric])
        heur.append(a[metric])
    if not diffs:
        return {}
    d = np.array(diffs)
    return {
        "n_partitions": int(d.size),
        "scp_mean": float(np.mean(scp)),
        "arm_mean": float(np.mean(heur)),
        "mean_gap": float(d.mean()),
        "median_gap": float(np.median(d)),
        "min_gap": float(d.min()),
        "max_gap": float(d.max()),
        "sd_gap": float(d.std(ddof=1)) if d.size > 1 else 0.0,
        "partitions_scp_ahead": int((d > 0).sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partitions", type=int, default=32)
    ap.add_argument("--inits", type=int, default=3)
    ap.add_argument("--max-scan", type=int, default=128)
    args = ap.parse_args()

    if not FROZEN.exists():
        raise SystemExit(f"HALT: {FROZEN} not found; nothing to compare against.")
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    frozen_by_seed = {r["seed"]: r for r in frozen["partitions"]}
    if OUT.exists():
        raise SystemExit(
            f"HALT: {OUT} already exists. Refusing to overwrite a result. "
            "Move or rename it first."
        )

    before = hash_protected()
    started = time.perf_counter()

    print("Drawing partitions under the frozen rules ...", flush=True)
    accepted, rejected = [], []
    p = 0
    while len(accepted) < args.partitions and p < args.max_scan:
        part = stratified_split_node_pools(p)
        (accepted if part.feasible else rejected).append(part if part.feasible else p)
        p += 1
    # The scan stops as soon as enough partitions are accepted, so compare
    # against the frozen rejections that fall inside the range scanned here.
    expected_rejected = [s for s in frozen["rejected_seeds"] if s < p]
    if rejected != expected_rejected:
        raise SystemExit(
            f"HALT: rejected seeds {rejected} differ from the frozen run's "
            f"{expected_rejected} over seeds 0..{p-1}. The partitioner has changed."
        )
    print(f"  accepted {len(accepted)} partitions; rejected {rejected} "
          f"(matches frozen over seeds 0..{p-1})")

    records = []
    for i, part in enumerate(accepted[: args.partitions]):
        t0 = time.perf_counter()
        rec = evaluate_partition(part, frozen_by_seed[part.seed], args.inits)
        records.append(rec)
        print(f"  [{i+1}/{args.partitions}] seed {part.seed}: "
              f"{'ok' if rec['usable'] else 'UNUSABLE (matches frozen)'} "
              f"({time.perf_counter()-t0:.0f}s)", flush=True)

    usable = [r for r in records if r.get("usable")]
    runtime = time.perf_counter() - started
    after = hash_protected()
    changed = [k for k in before if before[k] != after[k]]

    comparisons = {
        setname: {
            cfg.name: {m: paired(records, cfg.name, setname, m) for m in METRICS}
            for cfg in arms()
        }
        for setname in ("TEST-R", "TEST-H")
    }

    OUT.write_text(json.dumps({
        "purpose": "fixed-weight propagation heuristic vs frozen SCP-GNN",
        "frozen_source": FROZEN.name,
        "protocol": {
            "partitions_scanned": args.partitions,
            "partitions_analysed": len(usable),
            "inits_for_fitted_arm": args.inits,
            "subgraphs": {"train": N_TRAIN, "val": N_VAL, "test": N_TEST},
            "cascades": CASCADES, "epochs": EPOCHS,
            "nodes_per_subgraph": list(CFG.nodes_per_subgraph),
            "min_edges": CFG.min_edges,
            "reproduction_tolerance": REPRO_TOL,
            "primary_set": "TEST-R",
        },
        "arms": [{"name": c.name, "aggregator": c.aggregator, "rounds": c.rounds,
                  "trainable": c.trainable, "notes": c.notes} for c in arms()],
        "runtime_seconds": runtime,
        "protected_unchanged": not changed,
        "protected_changed": changed,
        "comparisons": comparisons,
        "partitions": records,
    }, indent=2), encoding="utf-8")

    # ----------------------------- report -----------------------------
    print(f"\nReproduction: all {len(usable)} partitions matched the frozen "
          f"baselines to < {REPRO_TOL:.0e}. The data is the frozen data.\n")

    print(f"TEST-R, mean over {len(usable)} partitions")
    print(f"{'model':22s}" + "".join(m.rjust(11) for m in METRICS))
    print("-" * (22 + 11 * len(METRICS)))
    scp = {m: _mean([r["scp_gnn_frozen"]["TEST-R"][m] for r in usable]) for m in METRICS}
    for cfg in arms():
        row = {m: _mean([r["metrics"][cfg.name]["TEST-R"][m] for r in usable])
               for m in METRICS}
        print(f"{cfg.name:22s}" + "".join(f"{row[m]:11.4f}" for m in METRICS))
    print(f"{'SCP-GNN (frozen)':22s}" + "".join(f"{scp[m]:11.4f}" for m in METRICS))

    print("\nPaired per-partition PR-AUC, SCP-GNN minus arm (TEST-R)")
    print(f"{'arm':22s}{'mean':>9}{'median':>9}{'min':>9}{'max':>9}{'SCP ahead':>12}")
    print("-" * 70)
    for cfg in arms():
        s = comparisons["TEST-R"][cfg.name]["pr_auc"]
        if not s:
            continue
        print(f"{cfg.name:22s}{s['mean_gap']:9.4f}{s['median_gap']:9.4f}"
              f"{s['min_gap']:9.4f}{s['max_gap']:9.4f}"
              f"{s['partitions_scp_ahead']:>8}/{s['n_partitions']}")

    print("\nFitted table (mean over partitions and inits), with the oracle beside it")
    fit_name = [c.name for c in arms() if c.trainable][0]
    from ..heuristic import CELL_NAMES, IR_MULTIPLIERS, ORACLE_WEIGHTS
    print(f"{'cell':28s}{'H-FIT':>9}{'oracle':>9}{'IncidentRelay':>15}")
    print("-" * 61)
    for cell in CELL_NAMES:
        fit = _mean([r["weight_tables"][fit_name][cell] for r in usable])
        print(f"{cell:28s}{fit:9.3f}{ORACLE_WEIGHTS[cell]:9.3f}"
              f"{IR_MULTIPLIERS[cell]:15.3f}")

    print(f"\nRuntime {runtime/60:.1f} min. Wrote {OUT}")
    print(f"Protected files unchanged: {not changed}"
          + (f"  CHANGED: {changed}" if changed else ""))


if __name__ == "__main__":
    main()
