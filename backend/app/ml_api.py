"""
Predicted blast radius - the ML endpoints.

RELATIONSHIP TO THE SQL ENDPOINTS
---------------------------------
/components/{id}/blast-radius          unchanged. Recursive CTE. Structure.
/components/{id}/blast-radius/predicted added here. Same reachable set, with
                                       a probability attached to each entry.

The predicted endpoint does NOT replace the SQL one and does not alter it.
Every component it lists is a component the recursive CTE already found - the
model's propagation rule assigns exactly zero to anything unreachable, so the
two sets are identical by construction and the model only ever reorders and
scores within it. A client that ignores the probability column gets the
existing answer.

GRACEFUL DEGRADATION
--------------------
If torch is not installed, or no model artifact has been trained, these
endpoints return HTTP 503 with an explanation of how to produce one. They
never fail the rest of the API: ml_api is mounted defensively in main.py so
that a broken or absent ML layer costs the deployment two endpoints rather
than the service.

The graph is rebuilt from MySQL on every request. It is 19 components here,
so the cost is two indexed SELECTs; caching it would introduce a staleness
bug the moment someone adds a dependency, which is a bad trade at this size.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path as PathParam, Query

from .db import query_all, query_one

# The `ml` package lives at the repository root, one level above `backend`.
# The API is normally started from inside backend/ (uvicorn app.main:app), so
# the root is not on sys.path and a plain `import ml` would fail.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

router = APIRouter()

MODEL_UNAVAILABLE = (
    "The prediction model is not available. Train one with: "
    "python -m ml.train_production"
)


def _load_ml():
    """Import the ML layer lazily.

    Deferred rather than imported at module scope so that starting the API
    without torch installed is a working API with two endpoints returning
    503, not a crash at import time.
    """
    try:
        from ml.graph import from_edges
        from ml.predict import ModelNotAvailable, load_predictor
    except ImportError as exc:  # torch / sklearn absent
        raise HTTPException(
            status_code=503,
            detail=f"{MODEL_UNAVAILABLE} (missing dependency: {exc})",
        ) from exc

    try:
        predictor = load_predictor()
    except ModelNotAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return from_edges, predictor


def _build_graph(from_edges):
    """Rebuild the dependency graph from the database."""
    components = query_all(
        "SELECT component_id, component_name, component_type, criticality, "
        "is_active FROM component ORDER BY component_id"
    )
    dependencies = query_all(
        "SELECT component_id, depends_on_id, dependency_type, is_critical "
        "FROM dependency ORDER BY dependency_id"
    )
    return from_edges(components, dependencies)


def _require_component(component_id: int) -> dict:
    row = query_one(
        "SELECT component_id, component_name FROM component WHERE component_id = %s",
        (component_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No component {component_id}")
    return row


@router.get("/components/{component_id}/blast-radius/predicted", tags=["ml"])
def predicted_blast_radius(
    component_id: Annotated[int, PathParam(ge=1)],
    threshold: Annotated[float, Query(ge=0.0, le=1.0)] = 0.5,
) -> dict[str, Any]:
    """
    Probabilistic blast radius: how likely each reachable component is to fail.

    The SQL blast radius answers "which components have a dependency path to
    this failure". This answers "and how likely is each of them to actually
    go down", by scoring every dependency edge on its type and criticality
    and composing those probabilities along the paths.

    Every row carries both: `reachable` is the SQL fact, `predicted_probability`
    is the model's estimate. `above_threshold` applies `threshold` so a caller
    can page on a chosen confidence instead of on reachability alone.

    LIMITS, returned in the payload so they cannot be lost:
      * The model is trained on synthetic graphs, not on this organisation's
        incident history. On the 22 recorded ShopSphere incidents it ranks
        substantially better than reachability but is not distinguishable
        from a simple depth heuristic; see docs/ml/experiments.md.
      * Probabilities are calibrated against synthetic data whose positive
        rate differs from the real one, so treat them as well-ordered
        confidences rather than exact frequencies.
      * Like the SQL it refines, it knows nothing about redundancy, failover
        or live health - none of that is in the schema.
    """
    component = _require_component(component_id)
    from_edges, predictor = _load_ml()
    graph = _build_graph(from_edges)

    try:
        rows = predictor.predict(graph, component_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    for r in rows:
        r["above_threshold"] = r["predicted_probability"] >= threshold

    explanations = [
        {
            "source_component_id": e.source_component_id,
            "source_name": e.source_name,
            "target_component_id": e.target_component_id,
            "target_name": e.target_name,
            "transmission_probability": e.transmission_probability,
            "dependency_features": e.dependency_features,
        }
        for e in predictor.explain(graph, component_id)
    ]

    provenance = predictor.metadata.get("provenance", {})
    return {
        "component_id": component_id,
        "component_name": component["component_name"],
        "threshold": threshold,
        "reachable_count": len(rows),
        "predicted_above_threshold": sum(1 for r in rows if r["above_threshold"]),
        "predictions": rows,
        "edge_transmission": explanations,
        "model": {
            "name": "SCP-GNN",
            "trained_at_utc": provenance.get("trained_at_utc"),
            "trained_on": provenance.get("data"),
        },
        "caveat": (
            "Model estimate, not an observation. The reachable set is computed "
            "in SQL and is unchanged; the probability is predicted. Trained on "
            "synthetic graphs - see docs/ml/experiments.md for measured limits."
        ),
    }


@router.get("/ml/model", tags=["ml"])
def model_info() -> dict[str, Any]:
    """Provenance and validation metrics of the currently loaded model."""
    _from_edges, predictor = _load_ml()
    provenance = predictor.metadata.get("provenance", {})
    config = predictor.metadata.get("config", {})
    return {
        "name": "SCP-GNN",
        "description": (
            "Semantic Cascade Propagation: learned per-edge transmission "
            "probabilities composed by noisy-OR along dependency paths."
        ),
        "config": config,
        "calibrated": predictor.metadata.get("calibrator") is not None,
        "provenance": provenance,
    }
