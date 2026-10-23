"""
SCP-GNN - Semantic Cascade Propagation network.

THE PROBLEM
-----------
Given a dependency graph and a component that has failed, estimate for every
other component the probability that it fails too.

The existing SQL answers a different question: which components are
*reachable* from the failure. Measured on the 22 recorded ShopSphere
incidents it flags 66 components where 37 were actually affected: precision
43.9%, recall 78.4%. Over half of what it pages is noise. The spread it is
blind to is large: on the same data, synchronous critical dependencies
carried failure 85.7% of the time while asynchronous non-critical ones
carried it 40% of the time.

Recall is 78.4% rather than 100% because 8 of those 37 impacts are on
components that are NOT reachable from the root cause: the failure travelled
from a component to something it depends on, not to something that depends
on it (incident 11, for instance, is an API Gateway misconfiguration that
degraded Order Service, which the Gateway calls). Reverse traversal cannot
express that, so 78.4% is a ceiling on recall for the SQL, for this model,
and for any other method built on the same traversal. See
docs/ml/experiments.md.

WHAT THIS MODEL DOES DIFFERENTLY
--------------------------------
A conventional GNN layer aggregates neighbour messages with a sum or mean
and passes the result through a nonlinearity; the hidden state is an
abstract embedding and the probability is read off a final classifier head.
Here the hidden state IS the failure probability, and the composition rule
is fixed to noisy-OR:

    q_v^(k) = 1 - (1 - q_v^(k-1)) * prod_{(s->v) in E} (1 - p_{s->v} * q_s^(k-1))

which is the marginal of an Independent Cascade process under the usual
independence assumption. The only learned object is the per-edge
transmission probability

    p_{s->v} = sigmoid( MLP_theta( [ x_s ; x_v ; z_{s->v} ] ) )

where x are node features and z are edge features (dependency type,
criticality). Three consequences follow, and they are the contribution:

1. INDUCTIVE, NOT TRANSDUCTIVE.
   Classical Independent Cascade learning (e.g. Wilinski & Lokhov 2020,
   arXiv:2007.06557) fits one free parameter per edge from many observed
   cascades on a fixed graph. A dependency graph changes every sprint and
   InfraTrace has 22 recorded incidents, so that approach cannot be applied
   at all: most edges have never been observed failing. Parameterising the
   transmission probability by edge *features* instead of edge *identity*
   makes the model independent of graph size and able to score an edge added
   five minutes ago.

   THIS IS NOT NEW. It is the hyperparametric Independent Cascade model of
   Kalimeris et al. (2018), applied to cascading failures by Xiang et al.
   (2024, arXiv:2406.08522) for power grids, with the same motivation. The
   only methodological difference here was an MLP in place of their linear
   sigma(theta^T x) - and the ablation in docs/ml/experiments.md shows that
   difference is worth nothing (+0.002 PR-AUC, p = 0.78), so a 28-parameter
   linear link (`linear_link=True`) performs identically to the 1,441-
   parameter MLP. Read docs/ml/novelty.md before describing any of this as
   novel; the contribution of this work is the evaluation and the measured
   recall ceiling, not the model.

2. THE OUTPUT IS BOUNDED BY REACHABILITY, PROVABLY.
   If v is unreachable from the root r then every term in the product above
   involves a q_s that is identically zero, so q_v = 0 exactly - not
   approximately. The model can therefore only ever *prune* the SQL's false
   positives; it can never invent an affected component that the recursive
   CTE did not already find. That matters operationally: adopting the model
   cannot lose a true positive that the current system would have caught,
   so its recall is bounded below by nothing worse than the reachability
   set it refines. test_scp.py asserts this property directly.

3. THE INTERMEDIATE QUANTITIES ARE THE EXPLANATION.
   p_{s->v} is a probability on a real dependency edge, so "Order Service is
   at 0.71 because its synchronous critical dependency on Payment Service
   transmits at 0.88" falls out of the forward pass. No post-hoc attribution
   method is involved.

WHAT THIS IS NOT
----------------
Nothing in this architecture is claimed as novel. Noisy-OR, Independent
Cascade, edge-conditioned message passing and feature-parameterised cascade
probabilities are all established, and the last of these - the actual core of
this model - has direct prior art in Kalimeris et al. (2018) and Xiang et al.
(2024). What this work contributes is empirical: a measured 78.4% structural
ceiling on recall imposed by dependency direction, and the finding that
message passing does not improve ranking over hand-engineered path features
on graphs this shallow. See docs/ml/novelty.md.

WHERE IT SHOULD FAIL
--------------------
Noisy-OR assumes each incoming dependency transmits independently. A
component that only falls over when a quorum of its dependencies fail is
outside the hypothesis class. ml/synth.py generates exactly such nodes on
purpose, and experiments/ sweeps their share so the breaking point is
measured rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
from torch import Tensor, nn

# Probabilities are clamped away from 0 and 1 so that log1p and the
# log-space survival recursion stay finite. 1e-6 is comfortably below any
# probability that would change an operational decision.
EPS = 1e-6
# Stands in for log(0) when a node is certain to have failed (the root).
NEG_LARGE = -30.0


@dataclass
class SCPConfig:
    """Hyperparameters. Defaults are the values used in the reported runs."""

    node_dim: int
    edge_dim: int
    hidden: int = 32
    rounds: int = 4          # K: propagation rounds; bounds the modelled path length
    dropout: float = 0.1
    use_edge_features: bool = True   # ablation: drop dependency type / criticality
    # Ablation that matters for the novelty claim: with linear_link=True the
    # transmission function collapses to sigma(theta^T x), which is exactly
    # the hyperparametric Independent Cascade model of Kalimeris et al.
    # (2018). If the MLP buys nothing over this, the only methodological
    # difference from that prior work is gone. See docs/ml/novelty.md.
    linear_link: bool = False


class EdgeTransmissionNet(nn.Module):
    """Maps (source features, target features, edge features) to a probability.

    Deliberately small. The quantity being learned is close to a lookup over
    dependency type and criticality, modulated by what the two endpoints are;
    a large network here would overfit the synthetic generator rather than
    learn that mapping.
    """

    def __init__(self, cfg: SCPConfig) -> None:
        super().__init__()
        in_dim = 2 * cfg.node_dim + (cfg.edge_dim if cfg.use_edge_features else 0)
        self.use_edge_features = cfg.use_edge_features
        if cfg.linear_link:
            # sigma(theta^T x): hyperparametric IC, Kalimeris et al. 2018.
            self.net = nn.Linear(in_dim, 1)
        else:
            self.net = nn.Sequential(
                nn.Linear(in_dim, cfg.hidden),
                nn.ReLU(),
                nn.Dropout(cfg.dropout),
                nn.Linear(cfg.hidden, cfg.hidden // 2),
                nn.ReLU(),
                nn.Linear(cfg.hidden // 2, 1),
            )

    def forward(self, x: Tensor, src: Tensor, dst: Tensor, edge_attr: Tensor) -> Tensor:
        """Return (E,) transmission probabilities in (0, 1)."""
        parts = [x[src], x[dst]]
        if self.use_edge_features:
            parts.append(edge_attr)
        logits = self.net(torch.cat(parts, dim=-1)).squeeze(-1)
        return torch.sigmoid(logits).clamp(EPS, 1.0 - EPS)


class SCPNet(nn.Module):
    """Semantic Cascade Propagation network.

    The forward pass is a fixed-point iteration, not a stack of learned
    layers: the same transmission probabilities are reused every round. Depth
    therefore costs nothing in parameters, and `rounds` can be raised at
    inference time to cover longer dependency chains than were seen in
    training.
    """

    def __init__(self, cfg: SCPConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.edge_net = EdgeTransmissionNet(cfg)

    def edge_probabilities(
        self, x: Tensor, src: Tensor, dst: Tensor, edge_attr: Tensor
    ) -> Tensor:
        """Per-edge transmission probabilities - the model's explanation."""
        return self.edge_net(x, src, dst, edge_attr)

    def forward(
        self,
        x: Tensor,
        src: Tensor,
        dst: Tensor,
        edge_attr: Tensor,
        roots: Tensor,
        rounds: int | None = None,
        aggregator: str = "noisy_or",
    ) -> Tensor:
        """
        Propagate failure from each root.

        Parameters
        ----------
        x:          (N, node_dim) node features.
        src, dst:   (E,) propagation edge endpoints; failure flows src -> dst.
        edge_attr:  (E, edge_dim) edge features.
        roots:      (B,) indices of the failed component, one per scenario.
        rounds:     overrides cfg.rounds; useful for testing convergence.
        aggregator: "noisy_or" (proposed) or "mean" (ablation). The ablation
                    keeps the learned per-edge probabilities and changes only
                    how they are combined, which is what isolates the
                    contribution of the composition rule.

        Returns
        -------
        (B, N) failure probabilities. Entry [b, roots[b]] is 1.
        """
        num_nodes = x.shape[0]
        batch = roots.shape[0]
        k_rounds = self.cfg.rounds if rounds is None else rounds
        p = self.edge_probabilities(x, src, dst, edge_attr)  # (E,)

        if aggregator == "mean":
            return self._forward_mean(p, src, dst, roots, num_nodes, k_rounds)

        # Survival in log space: log_surv[b, v] = log P(v has NOT failed).
        # Working in log space keeps the product over incoming edges stable
        # when a node has many dependencies.
        log_surv = torch.zeros(batch, num_nodes, device=x.device, dtype=p.dtype)
        root_onehot = torch.zeros_like(log_surv)
        root_onehot[torch.arange(batch, device=x.device), roots] = 1.0
        log_surv = log_surv.masked_fill(root_onehot.bool(), NEG_LARGE)

        p_e = p.unsqueeze(0)  # (1, E) broadcast across the batch
        for _ in range(k_rounds):
            q = (1.0 - torch.exp(log_surv)).clamp(0.0, 1.0 - EPS)  # (B, N)
            q_src = q[:, src]                                      # (B, E)
            # log(1 - p * q_src), clamped so the log argument stays positive.
            contrib = torch.log1p(-(p_e * q_src).clamp(max=1.0 - EPS))
            agg = torch.zeros_like(log_surv).index_add_(1, dst, contrib)
            # Survival is monotone: a node already more likely to have failed
            # cannot become less likely in a later round. Taking the minimum
            # of the running value and the new aggregate enforces that and
            # makes the iteration converge rather than oscillate.
            log_surv = torch.minimum(log_surv, agg)
            log_surv = log_surv.masked_fill(root_onehot.bool(), NEG_LARGE)

        q = (1.0 - torch.exp(log_surv)).clamp(EPS, 1.0)
        # The root is the premise, not a prediction.
        q = q.masked_fill(root_onehot.bool(), 1.0)
        return q

    def _forward_mean(
        self,
        p: Tensor,
        src: Tensor,
        dst: Tensor,
        roots: Tensor,
        num_nodes: int,
        k_rounds: int,
    ) -> Tensor:
        """Ablation: mean aggregation instead of noisy-OR.

        Same learned transmission probabilities, same number of rounds, same
        everything else - only the composition rule changes. Mean aggregation
        is what a conventional GNN does, and it has the wrong monotonicity:
        adding a second, weak failing dependency *lowers* the mean and so
        lowers the predicted probability, when physically it should raise it.
        """
        q = torch.zeros(roots.shape[0], num_nodes, device=p.device, dtype=p.dtype)
        q[torch.arange(roots.shape[0], device=p.device), roots] = 1.0
        root_onehot = q.clone().bool()

        deg = torch.zeros(num_nodes, device=p.device, dtype=p.dtype).index_add_(
            0, dst, torch.ones_like(p)
        ).clamp(min=1.0)

        p_e = p.unsqueeze(0)
        for _ in range(k_rounds):
            msg = p_e * q[:, src]
            agg = torch.zeros_like(q).index_add_(1, dst, msg) / deg.unsqueeze(0)
            q = torch.maximum(q, agg).clamp(0.0, 1.0)
            q = q.masked_fill(root_onehot, 1.0)
        return q.clamp(EPS, 1.0)
