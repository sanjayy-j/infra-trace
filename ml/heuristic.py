"""
Fixed-weight propagation heuristic: the incumbent practice, as a baseline.

WHY THIS EXISTS
---------------
`final-review/07-NOVELTY-AUDIT.md` recorded a gap in our own evidence. The
closest documented prior system, IncidentRelay, propagates impact through a
hand-authored **dependency multiplier table** keyed on the dependency's type
and criticality:

    hard/required 1.00   hard/important 0.85   soft/required 0.75
    soft/important 0.55  external/optional 0.20  informational 0.00
    (docs/concepts/services.md, "Dependency impact scoring")

Our 30-partition study never ran that heuristic. Its closest baseline was
logistic regression on path features, so the claim "a learned propagation
model beats hand-set edge weights" was unsupported. This module is the
missing baseline.

WHAT IT IS
----------
One torch module, deliberately with the same `forward` signature as SCPNet,
so it trains and scores through the *identical* loop in `ml/train.py` and the
identical metric code in `ml/evaluate.py`. The only difference from SCP-GNN
is the link function that produces the per-edge transmission probability:

    SCP-GNN       p_e = sigmoid(MLP([x_src ; x_dst ; z_e]))     1,441 params
    this module   p_e = table[dependency_type, is_critical]     8 cells

Everything downstream - the noisy-OR fixed point in log-survival space, the
round count, the monotone minimum, the root handling, the reachability mask -
is reproduced from `ml/models/scp.py` so that the comparison isolates the
link function and nothing else. `EPS` and `NEG_LARGE` are imported from that
module rather than re-declared, so the two cannot drift apart.

INFORMATION ASYMMETRY, STATED PLAINLY
-------------------------------------
This model sees strictly LESS than SCP-GNN: only the edge's type and
criticality, plus the topology. It cannot see component type, criticality,
is_active or degree, and it cannot condition an edge's probability on its
endpoints. That asymmetry is the point - it is what the incumbent heuristic
actually has - but it means a win for SCP-GNN is attributable to the
combination of richer input and learning, not to learning alone. The ORACLE
and FITTED arms below are what separate those two explanations.

COMBINATION RULES
-----------------
Two, both documented:

  noisy_or    1 - prod_e (1 - p_e * q_src)   -- SCP-GNN's rule, for parity
  max_product max_e (p_e * q_src)            -- IncidentRelay's documented
                                                rule: "Dependency propagation
                                                multiplies the upstream
                                                effective score"

Both are run for 4 rounds, the same as SCP-GNN. The real topology has maximum
depth 3 (docs/ml/topology-study.md), so 4 rounds covers every path in these
40-120 node subgraphs and no arm is handicapped by round count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch
from torch import Tensor, nn

from .graph import DEPENDENCY_TYPES
from .models.scp import EPS, NEG_LARGE

# Cell order is fixed and must never be reordered: index = type_index * 2 +
# is_critical, with a trailing cell for an edge whose dependency_type is
# outside the vocabulary.
CELL_NAMES: tuple[str, ...] = (
    *(f"{t}/{'critical' if c else 'non-critical'}"
      for t in DEPENDENCY_TYPES for c in (1, 0)),
    "unknown-type",
)
NUM_CELLS = len(CELL_NAMES)  # 9


# ---------------------------------------------------------------------
# The three weight tables. Each is a complete, auditable statement of the
# heuristic's assumptions, and each is sourced differently.
# ---------------------------------------------------------------------

# ARM 1 + 2 -- "IncidentRelay-style", documented multipliers.
#
# IncidentRelay's doc gives six multipliers by (dependency_type, criticality)
# where its types are {hard, soft, external, informational} and its
# criticalities are {required, important, optional}. Our schema's vocabulary
# is different, so a mapping is required. The mapping below is OUR
# assumption, not something IncidentRelay documents, and it is recorded here
# so a reviewer can disagree with it precisely:
#
#   Synchronous  -> hard      (a blocking call; the caller cannot proceed)
#   Data         -> hard      (a read/write path; same blocking character)
#   Asynchronous -> soft      (queued; the caller degrades rather than stops)
#   Config       -> external  (read at startup / refresh, not per request)
#   is_critical=1 -> required     is_critical=0 -> important
#
# Two cells are not covered by the six documented multipliers
# (external/required and external/important). IncidentRelay documents only
# external/optional = 0.20, so Config inherits 0.20 for both, which is
# recorded as an interpolation rather than a quoted figure.
IR_MULTIPLIERS: dict[str, float] = {
    "Synchronous/critical": 1.00,      # hard / required      (documented)
    "Synchronous/non-critical": 0.85,  # hard / important     (documented)
    "Data/critical": 1.00,             # hard / required      (documented)
    "Data/non-critical": 0.85,         # hard / important     (documented)
    "Asynchronous/critical": 0.75,     # soft / required      (documented)
    "Asynchronous/non-critical": 0.55, # soft / important     (documented)
    "Config/critical": 0.20,           # external / optional  (INTERPOLATED)
    "Config/non-critical": 0.20,       # external / optional  (INTERPOLATED)
    "unknown-type": 0.55,              # fallback (INTERPOLATED)
}

# ARM 3 -- ORACLE. These are the generator's own per-type transmission rates
# (ml/synth.SynthConfig.base_transmission), i.e. the exact table that
# produced the labels, up to the per-graph +/-15% jitter which no fixed table
# can know.
#
# THIS IS NOT A DEPLOYABLE BASELINE AND MUST NOT BE PRESENTED AS ONE. It is
# the upper bound on what ANY fixed (type x criticality) table could achieve
# on this substrate, and it exists to answer one question: how much of a
# heuristic's shortfall is the wrong numbers, and how much is the missing
# node context? SCP-GNN has to learn these rates from data; this arm is
# handed them.
ORACLE_WEIGHTS: dict[str, float] = {
    "Synchronous/critical": 0.88,
    "Synchronous/non-critical": 0.52,
    "Data/critical": 0.70,
    "Data/non-critical": 0.48,
    "Asynchronous/critical": 0.45,
    "Asynchronous/non-critical": 0.18,
    "Config/critical": 0.40,
    "Config/non-critical": 0.15,
    "unknown-type": 0.30,  # the generator's own default for an unknown key
}


def _table(mapping: dict[str, float]) -> np.ndarray:
    """Dict keyed by CELL_NAMES -> (NUM_CELLS,) array in canonical order."""
    missing = set(CELL_NAMES) - set(mapping)
    if missing:
        raise ValueError(f"weight table is missing cells: {sorted(missing)}")
    return np.array([mapping[name] for name in CELL_NAMES], dtype=np.float64)


@dataclass
class HeuristicConfig:
    """Everything that defines an arm. No field is tuned on test data."""

    name: str
    rounds: int = 4
    aggregator: str = "noisy_or"       # or "max_product"
    # Fixed arms supply `weights`; the fitted arm leaves it None and learns.
    weights: dict[str, float] | None = None
    trainable: bool = False
    # Logit used to initialise every cell of the fitted arm. 0.0 means every
    # edge type starts at p = 0.5, i.e. no prior ordering is injected.
    init_logit: float = 0.0
    notes: str = ""
    extras: dict = field(default_factory=dict)


def cell_selector(edge_attr: Tensor) -> Tensor:
    """(E, edge_dim) edge features -> (E, NUM_CELLS) one-hot cell selector.

    `edge_attr` is laid out by `ml.graph.from_edges` as
    [one-hot(DEPENDENCY_TYPES) , is_critical]. An all-zero one-hot means the
    dependency_type was outside the vocabulary, which routes to the trailing
    `unknown-type` cell rather than silently scoring zero.
    """
    num_types = len(DEPENDENCY_TYPES)
    type_oh = edge_attr[:, :num_types]
    is_crit = edge_attr[:, num_types]
    known = type_oh.sum(dim=-1) > 0.5

    sel = torch.zeros(
        edge_attr.shape[0], NUM_CELLS, device=edge_attr.device, dtype=edge_attr.dtype
    )
    # index = type * 2 + (0 if critical else 1), matching CELL_NAMES order.
    crit_offset = torch.where(is_crit > 0.5, 0.0, 1.0)
    for t in range(num_types):
        hit = (type_oh[:, t] > 0.5) & known
        if hit.any():
            idx = (2 * t + crit_offset[hit]).long()
            sel[torch.nonzero(hit, as_tuple=True)[0], idx] = 1.0
    sel[~known, NUM_CELLS - 1] = 1.0
    return sel


class FixedWeightPropagation(nn.Module):
    """Noisy-OR (or max-product) propagation over a per-edge-type weight table.

    Same `forward(x, src, dst, edge_attr, roots)` signature as SCPNet, so this
    trains through `ml.train.train_model` and scores through
    `ml.train.collect_predictions` with no change to either.

    `x` is accepted and deliberately ignored: this model has no access to node
    features. That is the information asymmetry documented in the module
    docstring, and ignoring the argument is how it is enforced rather than
    merely claimed.
    """

    def __init__(self, cfg: HeuristicConfig) -> None:
        super().__init__()
        self.cfg = cfg
        if cfg.trainable:
            if cfg.weights is not None:
                raise ValueError("a trainable arm must not be given fixed weights")
            self.logit = nn.Parameter(torch.full((NUM_CELLS,), float(cfg.init_logit)))
            self.register_buffer("fixed", torch.zeros(0))
        else:
            if cfg.weights is None:
                raise ValueError("a fixed arm needs a weight table")
            self.register_buffer(
                "fixed", torch.as_tensor(_table(cfg.weights), dtype=torch.float32)
            )

    # -----------------------------------------------------------------
    def edge_probabilities(self, edge_attr: Tensor) -> Tensor:
        """(E,) transmission probability per edge - the whole explanation."""
        sel = cell_selector(edge_attr)
        if self.cfg.trainable:
            p_cells = torch.sigmoid(self.logit)
        else:
            p_cells = self.fixed
        return (sel @ p_cells.to(sel.dtype)).clamp(EPS, 1.0 - EPS)

    def weight_table(self) -> dict[str, float]:
        """Current weights as an auditable dict - what a reviewer reads."""
        vals = (
            torch.sigmoid(self.logit) if self.cfg.trainable else self.fixed
        ).detach().cpu().numpy()
        return {name: float(v) for name, v in zip(CELL_NAMES, vals)}

    # -----------------------------------------------------------------
    def forward(
        self,
        x: Tensor,
        src: Tensor,
        dst: Tensor,
        edge_attr: Tensor,
        roots: Tensor,
        rounds: int | None = None,
        aggregator: str | None = None,
    ) -> Tensor:
        """(B, N) failure probabilities. Entry [b, roots[b]] is 1."""
        num_nodes = x.shape[0]
        batch = roots.shape[0]
        k_rounds = self.cfg.rounds if rounds is None else rounds
        rule = self.cfg.aggregator if aggregator is None else aggregator
        p = self.edge_probabilities(edge_attr)

        root_onehot = torch.zeros(
            batch, num_nodes, device=x.device, dtype=p.dtype
        )
        root_onehot[torch.arange(batch, device=x.device), roots] = 1.0
        root_bool = root_onehot.bool()

        if rule == "max_product":
            return self._max_product(p, src, dst, root_bool, num_nodes, batch, k_rounds)
        if rule != "noisy_or":
            raise ValueError(f"unknown aggregator {rule!r}")

        # --- noisy-OR in log-survival space, as in ml/models/scp.py ---
        log_surv = torch.zeros(batch, num_nodes, device=x.device, dtype=p.dtype)
        log_surv = log_surv.masked_fill(root_bool, NEG_LARGE)
        p_e = p.unsqueeze(0)
        for _ in range(k_rounds):
            q = (1.0 - torch.exp(log_surv)).clamp(0.0, 1.0 - EPS)
            contrib = torch.log1p(-(p_e * q[:, src]).clamp(max=1.0 - EPS))
            agg = torch.zeros_like(log_surv).index_add_(1, dst, contrib)
            log_surv = torch.minimum(log_surv, agg)
            log_surv = log_surv.masked_fill(root_bool, NEG_LARGE)

        q = (1.0 - torch.exp(log_surv)).clamp(EPS, 1.0)
        return q.masked_fill(root_bool, 1.0)

    def _max_product(
        self,
        p: Tensor,
        src: Tensor,
        dst: Tensor,
        root_bool: Tensor,
        num_nodes: int,
        batch: int,
        k_rounds: int,
    ) -> Tensor:
        """IncidentRelay's documented rule: multiply the upstream score.

        With several upstream dependencies the strongest path wins, which is
        what "the upstream effective score" means when a service has more
        than one unhealthy dependency. Unlike noisy-OR this is NOT
        super-additive: two independent weak paths never combine into a
        stronger one. That is a substantive modelling difference and it is
        exactly what this arm is here to measure.
        """
        q = torch.zeros(batch, num_nodes, device=p.device, dtype=p.dtype)
        q = q.masked_fill(root_bool, 1.0)
        for _ in range(k_rounds):
            cand = p.unsqueeze(0) * q[:, src]                      # (B, E)
            nxt = torch.zeros_like(q).index_reduce_(
                1, dst, cand, "amax", include_self=True
            )
            q = torch.maximum(q, nxt)        # monotone, as in the noisy-OR arm
            q = q.masked_fill(root_bool, 1.0)
        return q.clamp(EPS, 1.0).masked_fill(root_bool, 1.0)


# ---------------------------------------------------------------------
# The pre-registered arms. Fixed before any result was computed.
# ---------------------------------------------------------------------

def arms() -> list[HeuristicConfig]:
    """Four arms, each answering a different question."""
    return [
        HeuristicConfig(
            name="H-IR-noisyOR",
            weights=IR_MULTIPLIERS,
            aggregator="noisy_or",
            notes="IncidentRelay's documented multipliers, SCP's combination "
                  "rule. Isolates the weights by holding the rule fixed.",
        ),
        HeuristicConfig(
            name="H-IR-maxProduct",
            weights=IR_MULTIPLIERS,
            aggregator="max_product",
            notes="IncidentRelay as documented: its multipliers AND its "
                  "propagation rule. The deployable incumbent.",
        ),
        HeuristicConfig(
            name="H-ORACLE",
            weights=ORACLE_WEIGHTS,
            aggregator="noisy_or",
            notes="ORACLE - handed the generator's own per-type rates. Not "
                  "deployable. Upper bound for any fixed table here.",
        ),
        HeuristicConfig(
            name="H-FIT (8 params)",
            trainable=True,
            aggregator="noisy_or",
            notes="Fair information-matched arm: same loop, loss, epochs and "
                  "early stopping as SCP-GNN; train/val data only; 8 learned "
                  "cells instead of a 1,441-parameter MLP.",
        ),
    ]
