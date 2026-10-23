"""
Metrics for blast-radius prediction.

WHY THESE METRICS
-----------------
Accuracy is useless here. Most components are unaffected by most incidents,
so predicting "nothing fails" scores well and helps nobody.

PR-AUC is the headline number because it is threshold-free and insensitive
to the large negative class. Precision and recall are reported at a fixed
0.5 threshold because that is what an operator actually gets when they adopt
a model, and separately at the best-F1 threshold to show the attainable
trade-off.

Calibration is reported because the entire point of this model is that its
output is a probability, not a ranking. If "0.8" does not mean "fails about
80% of the time", then an on-call engineer cannot use it to decide whether
to page a team, and the method's main claim over the SQL has not been met.
A model can have excellent PR-AUC and be badly calibrated, so ranking
quality alone would not establish the claim.
"""

from __future__ import annotations

import numpy as np


def _safe(value: float) -> float:
    return float(value) if np.isfinite(value) else float("nan")


def expected_calibration_error(
    y_true: np.ndarray, y_score: np.ndarray, bins: int = 10
) -> float:
    """Equal-width-bin ECE: mean |confidence - accuracy| weighted by bin size."""
    if y_true.size == 0:
        return float("nan")
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        sel = (y_score > lo) & (y_score <= hi) if i else (y_score >= lo) & (y_score <= hi)
        if not sel.any():
            continue
        total += sel.mean() * abs(y_score[sel].mean() - y_true[sel].mean())
    return float(total)


def summarize(y_true: np.ndarray, y_score: np.ndarray, threshold: float = 0.5) -> dict:
    """Full metric set for one (y_true, y_score) pair."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    if y_true.size == 0:
        return {k: float("nan") for k in
                ("pr_auc", "roc_auc", "precision", "recall", "f1", "best_f1",
                 "best_threshold", "brier", "ece", "n", "positive_rate")}

    both_classes = len(np.unique(y_true)) > 1
    pred = (y_score >= threshold).astype(float)
    tp = float(((pred == 1) & (y_true == 1)).sum())
    fp = float(((pred == 1) & (y_true == 0)).sum())
    fn = float(((pred == 0) & (y_true == 1)).sum())

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # Best attainable F1 over all thresholds present in the scores.
    best_f1, best_thr = f1, threshold
    for thr in np.unique(np.round(y_score, 3)):
        p = (y_score >= thr).astype(float)
        tp_, fp_, fn_ = (
            float(((p == 1) & (y_true == 1)).sum()),
            float(((p == 1) & (y_true == 0)).sum()),
            float(((p == 0) & (y_true == 1)).sum()),
        )
        pr = tp_ / (tp_ + fp_) if (tp_ + fp_) else 0.0
        rc = tp_ / (tp_ + fn_) if (tp_ + fn_) else 0.0
        f = 2 * pr * rc / (pr + rc) if (pr + rc) else 0.0
        if f > best_f1:
            best_f1, best_thr = f, float(thr)

    return {
        "pr_auc": _safe(average_precision_score(y_true, y_score)) if both_classes else float("nan"),
        "roc_auc": _safe(roc_auc_score(y_true, y_score)) if both_classes else float("nan"),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "best_f1": best_f1,
        "best_threshold": best_thr,
        "brier": float(np.mean((y_score - y_true) ** 2)),
        "ece": expected_calibration_error(y_true, y_score),
        "n": int(y_true.size),
        "positive_rate": float(y_true.mean()),
    }


def aggregate(runs: list[dict]) -> dict:
    """Mean and standard deviation of each metric across seeds."""
    if not runs:
        return {}
    keys = runs[0].keys()
    out: dict[str, float] = {}
    for k in keys:
        vals = np.array([r[k] for r in runs], dtype=float)
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            out[f"{k}_mean"], out[f"{k}_std"] = float("nan"), float("nan")
        else:
            out[f"{k}_mean"] = float(vals.mean())
            out[f"{k}_std"] = float(vals.std(ddof=1)) if vals.size > 1 else 0.0
    return out


def format_table(rows: list[tuple[str, dict]], metrics: tuple[str, ...]) -> str:
    """Render a results table with mean +/- std, for the console and docs."""
    name_w = max(len(n) for n, _ in rows) + 2
    header = "model".ljust(name_w) + "".join(m.rjust(18) for m in metrics)
    lines = [header, "-" * len(header)]
    for name, res in rows:
        line = name.ljust(name_w)
        for m in metrics:
            mean, std = res.get(f"{m}_mean"), res.get(f"{m}_std")
            if mean is None or not np.isfinite(mean):
                line += "n/a".rjust(18)
            elif std is None or not np.isfinite(std) or std == 0.0:
                line += f"{mean:.3f}".rjust(18)
            else:
                line += f"{mean:.3f}+-{std:.3f}".rjust(18)
        lines.append(line)
    return "\n".join(lines)


def fit_isotonic(y_val: np.ndarray, p_val: np.ndarray):
    """Fit a monotone recalibration map on the VALIDATION split.

    Training with `pos_weight` deliberately distorts the output scale: it
    buys recall on a sparse positive class at the cost of predicting
    probabilities that are systematically too high. Isotonic regression
    repairs the scale.

    It is non-decreasing, so it never inverts the order of two predictions.
    It is NOT a relabelling that leaves ranking metrics untouched, though:
    it maps distinct scores onto shared values, and average precision treats
    ties differently from a strict ordering, so PR-AUC can move a little.
    The benchmark therefore reports pr_auc and pr_auc_calibrated separately
    instead of claiming they are equal - keeping the two claims, "ranks
    well" and "the number means something", from being conflated.

    Fitted on validation, never on test. Returns None when the split is
    degenerate, in which case the caller leaves the scores untouched.
    """
    from sklearn.isotonic import IsotonicRegression

    if y_val.size < 10 or len(np.unique(y_val)) < 2:
        return None
    # increasing=True is explicit and load-bearing. sklearn's default,
    # increasing="auto", infers the direction from the rank correlation of
    # the validation split and will happily fit a DECREASING map when that
    # correlation comes out negative - which on a small or noisy validation
    # split it can. That would mean "the model is more confident, so the
    # component is less likely to fail", silently inverting the output.
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(p_val, y_val)
    return iso
