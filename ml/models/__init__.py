"""Model implementations: the proposed SCP-GNN and its baselines."""

from .scp import SCPConfig, SCPNet
from .scp_bid import SCPBiDConfig, SCPBiDNet, bidirectional_tensors
from .baselines import (
    BidirectionalReachabilityBaseline,
    DepthDecayBaseline,
    GATNet,
    GCNNet,
    LogisticBaseline,
    ReachabilityBaseline,
    SAGENet,
)

__all__ = [
    "SCPConfig", "SCPNet",
    # Experimental bidirectional variant. Not deployed - it improves ranking
    # but loses F1 to SCP-GNN; see docs/ml/direction-study.md.
    "SCPBiDConfig", "SCPBiDNet", "bidirectional_tensors",
    "ReachabilityBaseline", "BidirectionalReachabilityBaseline",
    "DepthDecayBaseline", "LogisticBaseline",
    "GCNNet", "SAGENet", "GATNet",
]
