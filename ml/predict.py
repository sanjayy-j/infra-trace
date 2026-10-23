"""
Inference: load a trained SCP-GNN and score a real blast radius.

SEPARATION OF CONCERNS
----------------------
Every prediction this module returns is accompanied by the SQL-derived facts
it refines, and the two are never merged into one number:

    reachable            structure, from the recursive CTE. Ground truth
                         about the graph.
    hops_away            structure, from the same traversal.
    predicted_probability model output. An estimate, and labelled as one.

The API surfaces all three. An operator who distrusts the model can ignore
one field and still have exactly what the system produced before the model
existed - which is also what makes this safe to ship: the existing
behaviour is still right there, unchanged.

MODEL VERSIONING
----------------
The artifact stores the feature dimensions, the architecture config, the
training provenance and a calibrator. `load_predictor` refuses to load an
artifact whose feature dimensions disagree with the current ml/graph.py,
because silently feeding 11-dimensional features to a model trained on 12
produces plausible-looking nonsense rather than an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch

from .graph import (
    EDGE_FEATURE_DIM,
    EDGE_FEATURE_NAMES,
    NODE_FEATURE_DIM,
    Graph,
)
from .models import SCPConfig, SCPNet

DEFAULT_ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "scp_gnn.pt"


class ModelNotAvailable(RuntimeError):
    """Raised when no usable model artifact is present.

    The API catches this and degrades to SQL-only responses rather than
    failing the request: a missing model should cost you the prediction
    column, not the endpoint.
    """


@dataclass
class EdgeExplanation:
    """One dependency edge and the probability the model assigns to it."""

    source_component_id: int
    source_name: str
    target_component_id: int
    target_name: str
    transmission_probability: float
    dependency_features: dict[str, float]


class BlastRadiusPredictor:
    """Wraps a trained SCP-GNN for single-component inference."""

    def __init__(self, model: SCPNet, metadata: dict[str, Any]) -> None:
        self.model = model.eval()
        self.metadata = metadata
        self._calibrator = metadata.get("calibrator")

    # -----------------------------------------------------------------
    def _tensors(self, graph: Graph) -> dict[str, torch.Tensor]:
        return {
            "x": torch.as_tensor(graph.node_features, dtype=torch.float32),
            "src": torch.as_tensor(graph.src, dtype=torch.long),
            "dst": torch.as_tensor(graph.dst, dtype=torch.long),
            "edge_attr": torch.as_tensor(graph.edge_features, dtype=torch.float32),
        }

    def _calibrate(self, p: np.ndarray) -> np.ndarray:
        if self._calibrator is None:
            return p
        return np.asarray(self._calibrator.predict(p), dtype=np.float64)

    # -----------------------------------------------------------------
    def predict(self, graph: Graph, component_id: int) -> list[dict[str, Any]]:
        """
        Score every component against the failure of `component_id`.

        Returns one row per component in the reachable set, ordered by
        predicted probability. Unreachable components are omitted: the model
        assigns them exactly zero (see the bound property in models/scp.py),
        so listing them would pad the response with rows that carry no
        information.
        """
        root = graph.index_of(component_id)
        t = self._tensors(graph)
        roots = torch.tensor([root], dtype=torch.long)
        with torch.no_grad():
            q = self.model(t["x"], t["src"], t["dst"], t["edge_attr"], roots)[0].numpy()
        q = self._calibrate(q)

        reachable = graph.reachable_from(root)
        hops = graph.hop_distance_from(root)

        rows = [
            {
                "component_id": int(graph.node_ids[i]),
                "component_name": graph.names[i],
                "hops_away": int(hops[i]),
                "reachable": True,
                "predicted_probability": round(float(q[i]), 4),
            }
            for i in range(graph.num_nodes)
            if reachable[i]
        ]
        rows.sort(key=lambda r: (-r["predicted_probability"], r["hops_away"]))
        return rows

    # -----------------------------------------------------------------
    def explain(
        self, graph: Graph, component_id: int, top_k: int = 12
    ) -> list[EdgeExplanation]:
        """
        Per-edge transmission probabilities for edges inside the blast radius.

        These are the model's own intermediate quantities, not a post-hoc
        attribution: the forward pass multiplies exactly these numbers
        together. "Order Service is at 0.71 because the edge carrying the
        failure into it transmits at 0.83" is a literal description of the
        computation, which is why no SHAP-style explainer is involved.
        """
        root = graph.index_of(component_id)
        t = self._tensors(graph)
        with torch.no_grad():
            p = self.model.edge_probabilities(
                t["x"], t["src"], t["dst"], t["edge_attr"]
            ).numpy()

        reachable = graph.reachable_from(root)
        out: list[EdgeExplanation] = []
        for e in range(graph.num_edges):
            s, d = int(graph.src[e]), int(graph.dst[e])
            # Only edges that can actually carry this failure are relevant.
            if not (s == root or reachable[s]):
                continue
            out.append(
                EdgeExplanation(
                    source_component_id=int(graph.node_ids[s]),
                    source_name=graph.names[s],
                    target_component_id=int(graph.node_ids[d]),
                    target_name=graph.names[d],
                    transmission_probability=round(float(p[e]), 4),
                    dependency_features={
                        name: float(graph.edge_features[e][k])
                        for k, name in enumerate(EDGE_FEATURE_NAMES)
                        if graph.edge_features[e][k] != 0.0
                    },
                )
            )
        out.sort(key=lambda x: -x.transmission_probability)
        return out[:top_k]


_CACHE: dict[str, BlastRadiusPredictor] = {}


def load_predictor(path: Path | str = DEFAULT_ARTIFACT) -> BlastRadiusPredictor:
    """Load and cache a predictor. Raises ModelNotAvailable if unusable."""
    key = str(path)
    if key in _CACHE:
        return _CACHE[key]

    p = Path(path)
    if not p.exists():
        raise ModelNotAvailable(
            f"No model artifact at {p}. Train one with:\n"
            f"    python -m ml.train_production --epochs 150"
        )

    try:
        # weights_only=False is required because the artifact also carries the
        # fitted isotonic calibrator, which is a scikit-learn object rather
        # than a tensor. The file is produced by this repository's own
        # training script and is not user-supplied input.
        blob = torch.load(p, map_location="cpu", weights_only=False)
    except Exception as exc:  # pragma: no cover - corrupt artifact
        raise ModelNotAvailable(f"Could not read model artifact {p}: {exc}") from exc

    cfg = blob.get("config", {})
    if cfg.get("node_dim") != NODE_FEATURE_DIM or cfg.get("edge_dim") != EDGE_FEATURE_DIM:
        raise ModelNotAvailable(
            f"Model artifact {p} expects node/edge feature dimensions "
            f"{cfg.get('node_dim')}/{cfg.get('edge_dim')}, but ml/graph.py now "
            f"produces {NODE_FEATURE_DIM}/{EDGE_FEATURE_DIM}. The feature "
            "encoding changed since this model was trained; retrain it."
        )

    model = SCPNet(
        SCPConfig(
            node_dim=cfg["node_dim"],
            edge_dim=cfg["edge_dim"],
            hidden=cfg.get("hidden", 32),
            rounds=cfg.get("rounds", 4),
            # Defaults to False so artifacts written before this flag existed
            # still load as the MLP they were trained as.
            linear_link=cfg.get("linear_link", False),
        )
    )
    model.load_state_dict(blob["state_dict"])

    predictor = BlastRadiusPredictor(model, blob)
    _CACHE[key] = predictor
    return predictor
