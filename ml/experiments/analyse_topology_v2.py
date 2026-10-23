"""
Analysis for the corrected topology study. Written BEFORE the results existed.

Applies the frozen decision criteria from docs/ml/topology-study.md to
results/topology_v2.json. It computes nothing that was not specified in
advance, and it does not choose between metrics after the fact.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
No confidence interval claiming frequentist coverage is produced. All 32
partitions re-partition ONE 708-service graph, so partition-level values are
positively correlated by an unknown amount and no interval has valid coverage
for a population of graphs. Nominal paired-t and Wilcoxon values are printed
ONLY as flagged descriptives; they are anti-conservative and are not decision
criteria. Decisions rest on effect size, sign consistency and regression
checks, exactly as frozen.

TEST-R is PRIMARY. TEST-H is a SECONDARY stress set. They are reported in
separate blocks and never pooled or averaged together.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import stats

RESULTS = Path(__file__).resolve().parent / "results"

PRIMARY = "SCP-GNN"
BASELINE = "Logistic (path feats)"
MODELS = ["Reachability (SQL)", "Depth decay", "Logistic (path feats)",
          "GCN", "GraphSAGE", "GAT", "SCP-GNN"]

# Frozen criteria
GAP_THRESHOLD = 0.02
MIN_POSITIVE = 26          # of 32
MIN_PARTITION_GAP = -0.01
MAX_REGRESSION = 0.02


def gaps(records, setname, metric="pr_auc"):
    out = []
    for r in records:
        if not r.get("usable"):
            continue
        m = r["metrics"]
        if PRIMARY not in m or BASELINE not in m:
            continue
        if setname not in m[PRIMARY] or setname not in m[BASELINE]:
            continue
        out.append((r["seed"], m[PRIMARY][setname][metric] - m[BASELINE][setname][metric],
                    r.get("test_mean_hub_retention", 0.0)))
    return out


def describe(vals):
    a = np.asarray(vals, dtype=float)
    q1, q3 = np.percentile(a, [25, 75])
    return {"n": a.size, "median": float(np.median(a)), "mean": float(a.mean()),
            "iqr_low": float(q1), "iqr_high": float(q3),
            "min": float(a.min()), "max": float(a.max()),
            "fraction_positive": float((a > 0).mean()),
            "count_positive": int((a > 0).sum())}


def block(records, setname, label):
    print(f"\n{'='*78}\n{label}  [{setname}]\n{'='*78}")

    print(f"\n{'model':26}{'PR-AUC':>10}{'precision':>11}{'recall':>9}{'F1':>9}{'ECE':>9}")
    print("-" * 74)
    for name in MODELS:
        vals = {k: [] for k in ("pr_auc", "precision", "recall", "f1", "ece")}
        for r in records:
            if r.get("usable") and name in r["metrics"] and setname in r["metrics"][name]:
                for k in vals:
                    vals[k].append(r["metrics"][name][setname][k])
        if not vals["pr_auc"]:
            continue
        print(f"{name:26}" + "".join(
            f"{np.mean(vals[k]):>{w}.3f}" for k, w in
            (("pr_auc", 10), ("precision", 11), ("recall", 9), ("f1", 9), ("ece", 9))))

    g = gaps(records, setname)
    if not g:
        print("\n  no paired values available")
        return None
    seeds = [s for s, _, _ in g]
    vals = [v for _, v, _ in g]
    ret = [r for _, _, r in g]
    d = describe(vals)

    print(f"\n--- {PRIMARY} minus {BASELINE}, PR-AUC, per partition ---")
    for i in range(0, len(g), 8):
        chunk = g[i:i + 8]
        print("  seeds " + " ".join(f"{s:>3d}" for s, _, _ in chunk))
        print("  gap   " + " ".join(f"{v:>+.3f}"[:6].rjust(3) for _, v, _ in chunk))
    print(f"\n  all values: {[round(v,4) for v in vals]}")
    print(f"\n  n={d['n']}  median {d['median']:+.4f}  mean {d['mean']:+.4f}")
    print(f"  IQR [{d['iqr_low']:+.4f}, {d['iqr_high']:+.4f}]   "
          f"min {d['min']:+.4f}   max {d['max']:+.4f}")
    print(f"  positive in {d['count_positive']}/{d['n']} partitions "
          f"({d['fraction_positive']*100:.0f}%)")

    a = np.asarray(vals)
    try:
        w = stats.wilcoxon(a).pvalue
    except Exception:
        w = float("nan")
    t = stats.ttest_1samp(a, 0).pvalue
    print(f"\n  [DESCRIPTIVE ONLY - NOT A VALID TEST under partition dependence]")
    print(f"    nominal paired-t p={t:.4f}   Wilcoxon p={w:.4f}")
    print(f"    Partitions re-partition one graph; these are anti-conservative.")

    # hub-retention strata (frozen bands)
    print(f"\n--- gap by test-pool mean hub retention (frozen bands) ---")
    bands = [("<40%", -1, 0.40), ("40-70%", 0.40, 0.70), (">70%", 0.70, 2.0)]
    strata = {}
    for name, lo, hi in bands:
        sel = [v for v, r in zip(vals, ret) if lo < r <= hi]
        strata[name] = describe(sel) if sel else None
        if sel:
            print(f"  {name:8} n={len(sel):2d}  median {np.median(sel):+.4f}  "
                  f"positive {int(np.sum(np.array(sel)>0))}/{len(sel)}")
        else:
            print(f"  {name:8} n=0")
    return {"summary": d, "strata": strata, "seeds": seeds, "values": vals}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", type=Path, default=RESULTS / "topology_v2.json")
    args = ap.parse_args()

    data = json.loads(args.path.read_text(encoding="utf-8"))
    records = data["partitions"]
    usable = [r for r in records if r.get("usable")]

    print("=" * 78)
    print("CORRECTED TOPOLOGY STUDY - results")
    print("=" * 78)
    print(f"\nFrozen configuration: {json.dumps(data['frozen'])}")
    print(f"Rejected seeds (structural gate): {data['rejected_seeds'] or 'none'}")
    print(f"Partitions: {len(records)} drawn, {len(usable)} usable")
    print(f"Runtime: {data['runtime_seconds']/60:.1f} min")
    print(f"Protected files unchanged during run: {data['protected_unchanged']}")

    print("\n--- substrate ---")
    ps = [r["pool_sizes"] for r in usable]
    print(f"  pool sizes: train {np.mean([p['train'] for p in ps]):.0f}  "
          f"val {np.mean([p['val'] for p in ps]):.0f}  "
          f"test {np.mean([p['test'] for p in ps]):.0f}")
    print(f"  max pool deviation across partitions: "
          f"{max(max(r['deviation'].values()) for r in usable):.4f}")
    print(f"  test-pool hubs: mean {np.mean([r['test_hub_count'] for r in usable]):.1f}")
    print(f"  test max GLOBAL in-degree: "
          f"{max(r['test_max_global_in_degree'] for r in usable)} "
          f"(mean {np.mean([r['test_max_global_in_degree'] for r in usable]):.1f})")
    print(f"  test max RETAINED in-degree: "
          f"{max(r['test_max_retained_in_degree'] for r in usable)} "
          f"(mean {np.mean([r['test_max_retained_in_degree'] for r in usable]):.1f})")
    print(f"  mean hub retention in test: "
          f"{np.mean([r['test_mean_hub_retention'] for r in usable])*100:.0f}%")
    for s in ("TEST-R", "TEST-H"):
        cov = [r["coverage"][s] for r in usable if s in r["coverage"]]
        if cov:
            print(f"  {s}: {np.mean([c['subgraphs'] for c in cov]):.0f} subgraphs, "
                  f"{np.mean([c['distinct_services'] for c in cov]):.0f}/"
                  f"{cov[0]['pool_size']} services covered, "
                  f"mean pairwise Jaccard {np.mean([c['mean_pairwise_jaccard'] for c in cov]):.3f}")

    primary = block(usable, "TEST-R", "PRIMARY EVALUATION (uniform-seeded subgraphs)")
    secondary = block(usable, "TEST-H", "SECONDARY STRESS SET (hub-seeded) - diagnostic only")

    # per-hub reporting, worst and best retention
    print(f"\n{'='*78}\nPER-HUB REPORTING (test pool, first 3 partitions)\n{'='*78}")
    for r in usable[:3]:
        print(f"\n  partition seed {r['seed']}:")
        print(f"    {'node':>6}{'tier':>6}{'global':>8}{'retained':>10}{'retain%':>9}"
              f"{'cut':>6}{'cap':>6}")
        for h in r["test_hubs"][:8]:
            print(f"    {h['node']:>6}{h['tier']:>6}{h['global_in_degree']:>8}"
                  f"{h['retained_in_degree']:>10}{h['retention_fraction']*100:>8.0f}%"
                  f"{h['cut_edges']:>6}{h['observability_cap']:>6}")

    # frozen decision criteria
    print(f"\n{'='*78}\nFROZEN DECISION CRITERIA (applied to TEST-R only)\n{'='*78}")
    if primary:
        d = primary["summary"]
        reg = {}
        for metric in ("f1", "ece"):
            sc, lg = [], []
            for r in usable:
                m = r["metrics"]
                if "TEST-R" in m.get(PRIMARY, {}) and "TEST-R" in m.get(BASELINE, {}):
                    sc.append(m[PRIMARY]["TEST-R"][metric])
                    lg.append(m[BASELINE]["TEST-R"][metric])
            if sc:
                reg[metric] = float(np.mean(sc) - np.mean(lg))
        hi = primary["strata"].get(">70%")

        checks = [
            (f"median gap >= +{GAP_THRESHOLD}", d["median"] >= GAP_THRESHOLD,
             f"{d['median']:+.4f}"),
            (f">= {MIN_POSITIVE}/{d['n']} partitions positive",
             d["count_positive"] >= MIN_POSITIVE, f"{d['count_positive']}/{d['n']}"),
            (f"min partition gap > {MIN_PARTITION_GAP}", d["min"] > MIN_PARTITION_GAP,
             f"{d['min']:+.4f}"),
            ("gap present in >70% retention stratum",
             bool(hi) and hi["median"] > 0, f"{hi['median']:+.4f}" if hi else "n/a"),
            (f"F1 regression <= {MAX_REGRESSION}",
             reg.get("f1", 0) >= -MAX_REGRESSION, f"{reg.get('f1', float('nan')):+.4f}"),
            (f"ECE regression <= {MAX_REGRESSION}",
             reg.get("ece", 0) <= MAX_REGRESSION, f"{reg.get('ece', float('nan')):+.4f}"),
        ]
        for name, ok, detail in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name:46} {detail}")
        passed = all(ok for _, ok, _ in checks)
        print(f"\n  VERDICT: {'ADVANTAGE PERSISTS (conditional on this graph)' if passed else 'CRITERIA NOT MET'}")

    print(f"\n{'='*78}\nLIMITS OF INFERENCE\n{'='*78}")
    print("  * One graph, one organisation. The 32 partitions are re-partitions of")
    print("    the SAME 708-service graph; they are not independent systems.")
    print("  * No interval here has valid coverage for a population of graphs.")
    print("    No significance test was used as a decision criterion.")
    print("  * Cascades are SIMULATED on real structure. They are not observed")
    print("    production failures. ChronoGraph publishes no propagation labels.")
    print("  * Component types and criticality are synthetic; only wiring is real.")
    print("  * Hub presence is not hub preservation: retention is reported per hub.")
    print("  * Conclusions are conditional on this graph and do not generalise")
    print("    to other systems on this evidence.")


if __name__ == "__main__":
    main()
