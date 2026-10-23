"""
Incomplete-graph robustness: what happens when dependencies are missing?

SETUP
-----
Cascades are simulated on the COMPLETE graph. Edges are then hidden from the
model's input only, so the labels still describe what really happened -
including propagation through edges the model cannot see. Hiding edges before
simulating would change the ground truth and measure nothing.

Every model is scored on the same candidate set: components reachable on the
COMPLETE graph. That set does not depend on what any individual model can see,
so a model blinded by a missing edge takes the recall penalty rather than
being quietly excused from the decision.

TWO REMOVAL REGIMES
-------------------
Chandrasekhar et al. (2024, arXiv:2403.05704) prove the geometry of the
missingness decides the damage: uniform thinning is comparatively benign,
while losing bridge edges is catastrophic. Both are run, because a study of
random removal alone tests only the easy case.

THE PREDICTION UNDER TEST
-------------------------
DropEdge training cannot repair a structural zero. It changes what the model
learns, not the fact that at inference a node with no path has every noisy-OR
factor equal to 1 and an output of exactly 0. Only admitting candidate edges
can reach those components. SCP-GNN+DropEdge is the control for that claim.

Run:
    python -m ml.experiments.run_incomplete --seeds 10
    python -m ml.experiments.run_incomplete --seeds 3 --quick
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
from scipy import stats

from ..evaluate import aggregate, fit_isotonic, summarize
from ..graph import EDGE_FEATURE_DIM, NODE_FEATURE_DIM, Cascade, Graph
from ..incomplete import hide_edges, latent_candidate_pairs
from ..models import GCNNet, LogisticBaseline, ReachabilityBaseline, SCPConfig, SCPNet
from ..models.scp_bid import SCPBiDConfig, SCPBiDNet
from ..models.scp_latent import (
    LATENT_EDGE_FEATURE_DIM,
    SCPLatentConfig,
    SCPLatentNet,
)
from ..synth import generate_dataset
from ..train import TrainConfig, train_model

RESULTS = Path(__file__).resolve().parent / "results"

# A split entry: the complete graph (ground truth), the graph the model is
# allowed to see, and the cascades simulated on the complete one.
Split = list[tuple[Graph, Graph, list[Cascade]]]


def make_split(seed: int, n_graphs: int, frac: float, mode: str) -> Split:
    """Generate graphs, simulate on the complete ones, then hide edges."""
    rng = np.random.default_rng(seed)
    out: Split = []
    for complete, cascades in generate_dataset(seed, n_graphs, 6):
        observed, _keep = hide_edges(complete, frac, mode, rng)
        out.append((complete, observed, cascades))
    return out


def eval_mask(complete: Graph, cas: Cascade) -> np.ndarray:
    """Candidate set, fixed by the COMPLETE graph for every model alike."""
    return cas.eval_mask(complete, reachable_only=True)


# ---------------------------------------------------------------------
# Tensor builders - one per model family
# ---------------------------------------------------------------------
def _obs_tensors(observed: Graph):
    return {
        "x": torch.as_tensor(observed.node_features, dtype=torch.float32),
        "src": torch.as_tensor(observed.src, dtype=torch.long),
        "dst": torch.as_tensor(observed.dst, dtype=torch.long),
        "edge_attr": torch.as_tensor(observed.edge_features, dtype=torch.float32),
    }


def _bid_tensors(observed: Graph):
    return {
        "x": torch.as_tensor(observed.node_features, dtype=torch.float32),
        "src": torch.as_tensor(observed.bi_src, dtype=torch.long),
        "dst": torch.as_tensor(observed.bi_dst, dtype=torch.long),
        "edge_attr": torch.as_tensor(observed.bi_edge_features, dtype=torch.float32),
    }


def _latent_tensors(observed: Graph):
    attr = np.concatenate(
        [observed.edge_features, np.zeros((observed.num_edges, 1), dtype=np.float32)],
        axis=1,
    )
    cs, cd = latent_candidate_pairs(observed, top_k=0)
    return {
        "x": torch.as_tensor(observed.node_features, dtype=torch.float32),
        "src": torch.as_tensor(observed.src, dtype=torch.long),
        "dst": torch.as_tensor(observed.dst, dtype=torch.long),
        "edge_attr": torch.as_tensor(attr, dtype=torch.float32),
        "cand_src": torch.as_tensor(cs, dtype=torch.long),
        "cand_dst": torch.as_tensor(cd, dtype=torch.long),
    }


def _forward(model, t, roots, latent: bool):
    if latent:
        return model(t["x"], t["src"], t["dst"], t["edge_attr"], roots,
                     t["cand_src"], t["cand_dst"])
    return model(t["x"], t["src"], t["dst"], t["edge_attr"], roots)


def _drop_edges(t: dict, prob: float, latent: bool):
    """DropEdge: hide a random share of the OBSERVED edges for one step."""
    if prob <= 0 or t["src"].numel() == 0:
        return t
    keep = torch.rand(t["src"].shape[0]) >= prob
    if not keep.any():
        return t
    out = dict(t)
    out["src"], out["dst"] = t["src"][keep], t["dst"][keep]
    out["edge_attr"] = t["edge_attr"][keep]
    return out


# ---------------------------------------------------------------------
def train_and_score(
    ctor,
    train_split: Split,
    val_split: Split,
    test_split: Split,
    seed: int,
    epochs: int,
    tensor_fn,
    latent: bool = False,
    drop_edge: float = 0.0,
) -> dict:
    """One training run, scored on the complete-graph candidate set."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = ctor()
    opt = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=1e-4)
    loss_fn = torch.nn.BCELoss(reduction="none")

    prepared = []
    pos = neg = 0
    for complete, observed, cascades in train_split:
        if not cascades:
            continue
        masks = np.stack([eval_mask(complete, c) for c in cascades])
        labels = np.stack([c.labels for c in cascades])
        pos += int(labels[masks].sum())
        neg += int((labels[masks] < 0.5).sum())
        prepared.append((
            tensor_fn(observed),
            torch.tensor([c.root_index for c in cascades], dtype=torch.long),
            torch.as_tensor(labels, dtype=torch.float32),
            torch.as_tensor(masks, dtype=torch.bool),
        ))
    pos_weight = torch.tensor(float(np.clip(neg / max(pos, 1), 0.2, 20.0)))

    def evaluate(split: Split):
        model.eval()
        ys, ps = [], []
        with torch.no_grad():
            for complete, observed, cascades in split:
                if not cascades:
                    continue
                t = tensor_fn(observed)
                roots = torch.tensor([c.root_index for c in cascades], dtype=torch.long)
                q = _forward(model, t, roots, latent).numpy()
                for b, c in enumerate(cascades):
                    m = eval_mask(complete, c)
                    if not m.any():
                        continue
                    ys.append(c.labels[m])
                    ps.append(q[b][m])
        if not ys:
            return np.array([]), np.array([])
        return np.concatenate(ys), np.concatenate(ps)

    best, best_state, since = -np.inf, None, 0
    started = time.perf_counter()
    for _ in range(epochs):
        model.train()
        for t, roots, labels, masks in prepared:
            if not masks.any():
                continue
            opt.zero_grad()
            q = _forward(model, _drop_edges(t, drop_edge, latent), roots, latent)
            raw = loss_fn(q.clamp(1e-6, 1 - 1e-6), labels)
            w = torch.where(labels > 0.5, pos_weight, torch.ones_like(labels))
            ((raw * w)[masks].mean()).backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()

        y, p = evaluate(val_split)
        from ..train import pr_auc

        score = pr_auc(y, p)
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
    train_s = time.perf_counter() - started

    t0 = time.perf_counter()
    y, p = evaluate(test_split)
    infer_s = time.perf_counter() - t0

    res = summarize(y, p)
    res["params"] = sum(q.numel() for q in model.parameters())
    res["train_seconds"] = train_s
    res["inference_ms_per_scenario"] = 1000.0 * infer_s / max(
        sum(len(c) for _, _, c in test_split), 1
    )
    y_val, p_val = evaluate(val_split)
    iso = fit_isotonic(y_val, p_val)
    if iso is not None:
        res["ece_calibrated"] = summarize(y, np.asarray(iso.predict(p)))["ece"]
    return res


def classical_score(model, split: Split, fit_split: Split | None = None) -> dict:
    if fit_split is not None and hasattr(model, "fit"):
        model.fit([(obs, cas) for _c, obs, cas in fit_split])
    ys, ps = [], []
    for complete, observed, cascades in split:
        for c in cascades:
            m = eval_mask(complete, c)
            if not m.any():
                continue
            ys.append(c.labels[m])
            ps.append(model.predict(observed, c.root_index)[m])
    if not ys:
        return summarize(np.array([]), np.array([]))
    return summarize(np.concatenate(ys), np.concatenate(ps))


def run_seed(seed: int, frac: float, mode: str, quick: bool, epochs: int) -> dict:
    n_tr, n_va, n_te = (16, 6, 12) if quick else (60, 16, 32)
    tr = make_split(seed * 1000 + 1, n_tr, frac, mode)
    va = make_split(seed * 1000 + 2, n_va, frac, mode)
    te = make_split(seed * 1000 + 3, n_te, frac, mode)
    out: dict[str, dict] = {}

    out["Reachability (observed)"] = classical_score(ReachabilityBaseline(), te)
    out["Logistic (path feats)"] = classical_score(LogisticBaseline(), te, tr)

    out["GCN"] = train_and_score(
        lambda: GCNNet(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4),
        tr, va, te, seed, epochs, _obs_tensors)

    out["SCP-GNN (default)"] = train_and_score(
        lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        tr, va, te, seed, epochs, _obs_tensors)

    out["SCP-BiD (experimental)"] = train_and_score(
        lambda: SCPBiDNet(SCPBiDConfig(NODE_FEATURE_DIM, hidden=32, rounds=4)),
        tr, va, te, seed, epochs, _bid_tensors)

    out["SCP-GNN + DropEdge 0.2"] = train_and_score(
        lambda: SCPNet(SCPConfig(NODE_FEATURE_DIM, EDGE_FEATURE_DIM, 32, 4)),
        tr, va, te, seed, epochs, _obs_tensors, drop_edge=0.2)

    out["SCP-Latent (proposed)"] = train_and_score(
        lambda: SCPLatentNet(SCPLatentConfig(NODE_FEATURE_DIM, top_k=2)),
        tr, va, te, seed, epochs, _latent_tensors, latent=True)

    out["  ablation: random scoring"] = train_and_score(
        lambda: SCPLatentNet(
            SCPLatentConfig(NODE_FEATURE_DIM, top_k=2, random_scoring=True)),
        tr, va, te, seed, epochs, _latent_tensors, latent=True)

    out["  ablation: top_k=0"] = train_and_score(
        lambda: SCPLatentNet(SCPLatentConfig(NODE_FEATURE_DIM, top_k=0)),
        tr, va, te, seed, epochs, _latent_tensors, latent=True)

    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--fractions", type=float, nargs="+", default=[0.0, 0.1, 0.2, 0.3])
    ap.add_argument("--modes", nargs="+", default=["random", "bridge"])
    args = ap.parse_args()

    everything: dict = {}
    for mode in args.modes:
        for frac in args.fractions:
            if frac == 0.0 and mode != args.modes[0]:
                continue  # 0% removal is identical in every regime
            key = f"{mode} {int(frac*100)}%"
            per_seed: dict[str, list[dict]] = {}
            for seed in range(args.seeds):
                print(f"--- {key} seed {seed} ---", flush=True)
                for name, res in run_seed(seed, frac, mode, args.quick, args.epochs).items():
                    per_seed.setdefault(name, []).append(res)
            everything[key] = {"per_seed": per_seed,
                               "summary": {k: aggregate(v) for k, v in per_seed.items()}}

            print(f"\n=== {key} ===")
            print(f"{'model':34}{'PR-AUC':>16}{'prec':>9}{'recall':>9}{'F1':>16}")
            print("-" * 84)
            for name, runs in per_seed.items():
                a = aggregate(runs)
                print(f"{name:34}"
                      f"{a['pr_auc_mean']:>10.3f}+-{a['pr_auc_std']:.3f}"
                      f"{a['precision_mean']:>9.3f}{a['recall_mean']:>9.3f}"
                      f"{a['f1_mean']:>10.3f}+-{a['f1_std']:.3f}")

            base = "SCP-Latent (proposed)"
            print(f"\n  paired vs {base}:")
            for other in per_seed:
                if other == base:
                    continue
                for m in ("pr_auc", "f1"):
                    a = np.array([r[m] for r in per_seed[base]], dtype=float)
                    b = np.array([r[m] for r in per_seed[other]], dtype=float)
                    ok = np.isfinite(a) & np.isfinite(b)
                    a, b = a[ok], b[ok]
                    if a.size < 2 or np.allclose(a, b):
                        continue
                    d = a - b
                    pv = stats.ttest_rel(a, b).pvalue
                    mark = "***" if pv < 0.001 else "**" if pv < 0.01 else "*" if pv < 0.05 else " ns"
                    print(f"    {other.strip():32} {m:7} {d.mean():+.4f} p={pv:.4f}{mark} "
                          f"{int((d>0).sum())}/{d.size}")
            print()

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "incomplete.json"
    path.write_text(json.dumps(everything, indent=2), encoding="utf-8")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
