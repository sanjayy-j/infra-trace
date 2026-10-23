"""
Incomplete-graph robustness on the 22 real incidents.

Edges are hidden from the REAL dependency graph; labels come from the real
incident records, which describe what happened on the complete graph. Models
are trained on synthetic data (with the same removal regime applied) and never
on these incidents.

SAMPLE SIZE
-----------
22 incidents, 66 reverse-reachable decisions on the complete graph. At 30%
removal only about 20 of the 29 edges remain. Every number here carries a wide
bootstrap interval and none of it settles anything on its own; it is a check
that the synthetic conclusion is not contradicted by the only real data
available, not independent evidence.

Run:
    python -m ml.experiments.run_incomplete_real --seeds 3
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from ..evaluate import summarize
from ..extract import load_graph, load_incident_cascades
from ..graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM
from ..incomplete import hide_edges
from ..models import LogisticBaseline, ReachabilityBaseline, SCPConfig, SCPNet
from ..models.scp_latent import SCPLatentConfig, SCPLatentNet
from .run_incomplete import _latent_tensors, _obs_tensors, eval_mask, make_split

RESULTS = Path(__file__).resolve().parent / "results"


def bootstrap_ci(scen, metric, n_boot=2000, seed=0):
    rng = np.random.default_rng(seed)
    point = summarize(
        np.concatenate([y for y, _ in scen]), np.concatenate([p for _, p in scen])
    )[metric]
    vals = []
    idx = np.arange(len(scen))
    for _ in range(n_boot):
        pick = rng.choice(idx, size=idx.size, replace=True)
        v = summarize(
            np.concatenate([scen[i][0] for i in pick]),
            np.concatenate([scen[i][1] for i in pick]),
        )[metric]
        if np.isfinite(v):
            vals.append(v)
    if not vals:
        return point, float("nan"), float("nan")
    return point, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--fractions", type=float, nargs="+", default=[0.0, 0.2])
    ap.add_argument("--modes", nargs="+", default=["random", "bridge"])
    args = ap.parse_args()

    complete = load_graph()
    cascades = load_incident_cascades(complete)
    print(f"Real graph N={complete.num_nodes} E={complete.num_edges}, "
          f"{len(cascades)} incidents")

    out: dict = {}
    for mode in args.modes:
        for frac in args.fractions:
            if frac == 0.0 and mode != args.modes[0]:
                continue
            key = f"{mode} {int(frac*100)}%"
            rng = np.random.default_rng(0)
            observed, keep = hide_edges(complete, frac, mode, rng)
            ceiling_pos = ceiling_tot = 0
            for c in cascades:
                m = eval_mask(complete, c)
                ceiling_tot += int(c.labels[m].sum())
                ceiling_pos += int(
                    (c.labels[m] * observed.reachable_from(c.root_index)[m]).sum()
                )
            print(f"\n=== {key}: {complete.num_edges} -> {observed.num_edges} edges, "
                  f"recall ceiling {ceiling_pos}/{ceiling_tot} = "
                  f"{ceiling_pos/max(ceiling_tot,1)*100:.1f}% ===")

            rows: dict[str, list] = {}

            def classical(model):
                scen = []
                for c in cascades:
                    m = eval_mask(complete, c)
                    if not m.any():
                        continue
                    scen.append((c.labels[m], model.predict(observed, c.root_index)[m]))
                return scen

            rows["Reachability (observed)"] = classical(ReachabilityBaseline())

            # Train on synthetic with the SAME removal regime.
            tr = make_split(901, 60, frac, mode)
            va = make_split(902, 16, frac, mode)
            rows["Logistic (path feats)"] = classical(
                LogisticBaseline().fit([(o, c) for _c, o, c in tr])
            )

            for name, ctor, tfn, latent in (
                ("SCP-GNN (default)",
                 lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
                 _obs_tensors, False),
                ("SCP-Latent (proposed)",
                 lambda: SCPLatentNet(SCPLatentConfig(NODE_FEATURE_DIM, top_k=2)),
                 _latent_tensors, True),
            ):
                stacked = []
                from .run_incomplete import _forward

                for seed in range(args.seeds):
                    # _fit_only rather than train_and_score: the latter returns
                    # metrics and discards the weights, and we need the fitted
                    # model to score against the real incidents.
                    fitted = _fit_only(ctor, tr, va, seed, args.epochs, tfn, latent)
                    t = tfn(observed)
                    preds = []
                    for c in cascades:
                        m = eval_mask(complete, c)
                        if not m.any():
                            continue
                        with torch.no_grad():
                            q = _forward(
                                fitted, t,
                                torch.tensor([c.root_index], dtype=torch.long), latent
                            )[0].numpy()
                        preds.append(q[m])
                    stacked.append(preds)
                ys = [c.labels[eval_mask(complete, c)] for c in cascades
                      if eval_mask(complete, c).any()]
                rows[name] = [
                    (ys[i], np.mean([s[i] for s in stacked], axis=0))
                    for i in range(len(ys))
                ]

            header = f"{'model':30}{'PR-AUC (95% CI)':>26}{'prec':>8}{'rec':>8}{'F1':>8}"
            print(header)
            print("-" * len(header))
            res: dict[str, dict] = {}
            for name, scen in rows.items():
                y = np.concatenate([a for a, _ in scen])
                p = np.concatenate([b for _, b in scen])
                m = summarize(y, p)
                pt, lo, hi = bootstrap_ci(scen, "pr_auc")
                ci = f"{pt:.3f} [{lo:.3f}, {hi:.3f}]" if np.isfinite(lo) else f"{pt:.3f}"
                print(f"{name:30}{ci:>26}{m['precision']:>8.3f}"
                      f"{m['recall']:>8.3f}{m['f1']:>8.3f}")
                res[name] = {**{k: v for k, v in m.items() if isinstance(v, (int, float))},
                             "pr_auc_ci_low": lo, "pr_auc_ci_high": hi}
            out[key] = {"edges_left": int(observed.num_edges),
                        "recall_ceiling": ceiling_pos / max(ceiling_tot, 1),
                        "results": res}

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "incomplete_real.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nn=22 incidents. Intervals are wide and overlap; this is a check that")
    print("the synthetic conclusion is not contradicted, not independent evidence.")
    print(f"\nWrote {path}")


def _fit_only(ctor, tr, va, seed, epochs, tensor_fn, latent):
    """Train a model and return it (train_and_score returns metrics, not weights)."""
    from .run_incomplete import _drop_edges, _forward
    from ..train import pr_auc

    torch.manual_seed(seed)
    np.random.seed(seed)
    model = ctor()
    opt = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=1e-4)
    loss_fn = torch.nn.BCELoss(reduction="none")

    prepared, pos, neg = [], 0, 0
    for comp, obs, cas in tr:
        if not cas:
            continue
        masks = np.stack([eval_mask(comp, c) for c in cas])
        labels = np.stack([c.labels for c in cas])
        pos += int(labels[masks].sum())
        neg += int((labels[masks] < 0.5).sum())
        prepared.append((tensor_fn(obs),
                         torch.tensor([c.root_index for c in cas], dtype=torch.long),
                         torch.as_tensor(labels, dtype=torch.float32),
                         torch.as_tensor(masks, dtype=torch.bool)))
    pw = torch.tensor(float(np.clip(neg / max(pos, 1), 0.2, 20.0)))

    best, best_state, since = -np.inf, None, 0
    for _ in range(epochs):
        model.train()
        for t, roots, labels, masks in prepared:
            if not masks.any():
                continue
            opt.zero_grad()
            q = _forward(model, t, roots, latent)
            raw = loss_fn(q.clamp(1e-6, 1 - 1e-6), labels)
            w = torch.where(labels > 0.5, pw, torch.ones_like(labels))
            ((raw * w)[masks].mean()).backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
        model.eval()
        ys, ps = [], []
        with torch.no_grad():
            for comp, obs, cas in va:
                if not cas:
                    continue
                t = tensor_fn(obs)
                roots = torch.tensor([c.root_index for c in cas], dtype=torch.long)
                q = _forward(model, t, roots, latent).numpy()
                for b, c in enumerate(cas):
                    m = eval_mask(comp, c)
                    if m.any():
                        ys.append(c.labels[m])
                        ps.append(q[b][m])
        score = pr_auc(np.concatenate(ys), np.concatenate(ps)) if ys else np.nan
        score = -np.inf if np.isnan(score) else score
        if score > best:
            best, since = score, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            since += 1
            if since >= 20:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    return model


if __name__ == "__main__":
    main()
