"""
Graph container and feature encoding for the InfraTrace ML layer.

Design rule inherited from backend/app/db.py: the DATABASE is the source of
truth. This module does NOT reimplement blast radius or risk scoring. It
converts whatever graph it is given - real (from MySQL) or synthetic - into
dense arrays a model can consume, and nothing more.

DIRECTION, STATED ONCE
----------------------
A `dependency` row (component_id = u, depends_on_id = v) reads
"u depends on v". Therefore a FAILURE of v propagates TO u.

The dependency edge and the propagation edge point in opposite directions:

    dependency edge   u --depends on--> v
    propagation edge  v --failure----->  u

Everything in this package works on PROPAGATION edges, stored as
(src = depends_on_id, dst = component_id). Getting this backwards inverts
every prediction, so the conversion happens exactly once, in `from_edges`.
This matches the reverse traversal used by the recursive CTE in
database/queries/07_blast_radius.sql.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------
# Vocabularies.
#
# These mirror the CHECK constraints in database/schema/schema.sql. They are
# fixed and ordered so that a model trained today can be loaded tomorrow and
# still interpret feature column 3 as the same thing. Never reorder these;
# append only.
# ---------------------------------------------------------------------
COMPONENT_TYPES: tuple[str, ...] = (
    "Service",
    "Database",
    "Cache",
    "Queue",
    "Storage",
    "Gateway",
    "ExternalAPI",
)

DEPENDENCY_TYPES: tuple[str, ...] = (
    "Synchronous",
    "Asynchronous",
    "Data",
    "Config",
)

# Ordinal, scaled to (0, 1]. Criticality is an ordered scale, so a single
# ordinal column carries the ordering that four one-hot columns would throw
# away.
CRITICALITY_SCALE: dict[str, float] = {
    "Low": 0.25,
    "Medium": 0.50,
    "High": 0.75,
    "Critical": 1.00,
}

NODE_FEATURE_DIM = len(COMPONENT_TYPES) + 4   # one-hot + crit + active + 2 degrees
EDGE_FEATURE_DIM = len(DEPENDENCY_TYPES) + 1  # one-hot + is_critical

NODE_FEATURE_NAMES: tuple[str, ...] = (
    *(f"type={t}" for t in COMPONENT_TYPES),
    "criticality",
    "is_active",
    "in_degree_norm",
    "out_degree_norm",
)

EDGE_FEATURE_NAMES: tuple[str, ...] = (
    *(f"dep_type={t}" for t in DEPENDENCY_TYPES),
    "is_critical",
)

# ---------------------------------------------------------------------
# Bidirectional propagation (see docs/ml/direction-study.md)
#
# The arrays above describe REVERSE propagation only: a dependency fails and
# its dependents suffer. That is what the recursive CTE models and what
# SCP-GNN consumes, and it is left exactly as it was.
#
# Measured on the 22 recorded incidents, 7 of 37 impacts are reachable only
# by following a dependency edge FORWARDS - a component damaging something it
# depends on (retry storm, producer pollution, cache churn). Those edges carry
# failure at 31.8% against 61.9% for reverse edges, so they are neither
# negligible nor equivalent.
#
# The bi_* arrays below carry both directions, with one extra feature marking
# which. Everything else - the model class, the composition rule, the loss -
# is unchanged; the whole modification is this flag.
# ---------------------------------------------------------------------
DIRECTION_REVERSE = 0.0   # dependency fails -> dependent suffers
DIRECTION_FORWARD = 1.0   # dependent fails  -> dependency suffers

BI_EDGE_FEATURE_DIM = EDGE_FEATURE_DIM + 1
BI_EDGE_FEATURE_NAMES: tuple[str, ...] = (*EDGE_FEATURE_NAMES, "is_forward")


def _one_hot(value: str, vocab: tuple[str, ...]) -> list[float]:
    """One-hot encode, tolerating unseen values as an all-zero vector.

    An unknown component_type should degrade to "no type information"
    rather than crash inference on a component added after the model was
    trained. The CHECK constraints make this unlikely, but a model that is
    loaded months later should not be the thing that takes the API down.
    """
    vec = [0.0] * len(vocab)
    if value in vocab:
        vec[vocab.index(value)] = 1.0
    return vec


@dataclass
class Graph:
    """
    A dependency graph in the form the models consume.

    Attributes
    ----------
    node_ids:
        Original component_id for each row of `node_features`. Index i in
        every array in this class refers to `node_ids[i]`.
    node_features:
        (N, NODE_FEATURE_DIM) float32.
    src, dst:
        (E,) int64 PROPAGATION edge endpoints: failure flows src -> dst.
    edge_features:
        (E, EDGE_FEATURE_DIM) float32.
    names:
        Human-readable component names, for explanations. Index-aligned.
    """

    node_ids: np.ndarray
    node_features: np.ndarray
    src: np.ndarray
    dst: np.ndarray
    edge_features: np.ndarray
    names: list[str] = field(default_factory=list)
    # Bidirectional view: every dependency row contributes two propagation
    # edges, distinguished by the trailing `is_forward` feature. Populated by
    # `from_edges`; consumed only by SCP-BiD. Reverse edges come first, so
    # bi_src[:num_edges] == src.
    bi_src: np.ndarray | None = None
    bi_dst: np.ndarray | None = None
    bi_edge_features: np.ndarray | None = None

    @property
    def num_nodes(self) -> int:
        return int(self.node_features.shape[0])

    @property
    def num_edges(self) -> int:
        return int(self.src.shape[0])

    def index_of(self, component_id: int) -> int:
        """Map a database component_id to its row index."""
        hits = np.nonzero(self.node_ids == component_id)[0]
        if hits.size == 0:
            raise KeyError(f"component_id {component_id} is not in this graph")
        return int(hits[0])

    # -----------------------------------------------------------------
    # Reachability - the structural upper bound.
    #
    # This is the pure-Python equivalent of the recursive CTE in
    # database/queries/07_blast_radius.sql. It exists here ONLY so that
    # experiments can run on synthetic graphs that are not in MySQL, and so
    # that the safety property (prediction <= reachability) can be checked
    # directly. The database remains the source of truth for the real graph;
    # ml/extract.py verifies that this function agrees with the SQL.
    # -----------------------------------------------------------------
    def reachable_from(self, root_index: int) -> np.ndarray:
        """Boolean mask of nodes reachable from `root_index`, root excluded.

        Breadth-first over propagation edges. Terminates on cyclic graphs
        because a node is enqueued at most once - the same reason the SQL
        uses UNION rather than UNION ALL.
        """
        seen = np.zeros(self.num_nodes, dtype=bool)
        seen[root_index] = True
        frontier = [root_index]
        # Adjacency built per call; graphs here are small (tens to low
        # thousands of nodes) and this keeps Graph immutable and picklable.
        adj: dict[int, list[int]] = {}
        for e in range(self.num_edges):
            adj.setdefault(int(self.src[e]), []).append(int(self.dst[e]))

        while frontier:
            node = frontier.pop()
            for nxt in adj.get(node, ()):
                if not seen[nxt]:
                    seen[nxt] = True
                    frontier.append(nxt)

        seen[root_index] = False  # a component is not in its own blast radius
        return seen

    def reachable_bidirectional(self, root_index: int) -> np.ndarray:
        """Reachability following dependency edges in EITHER direction.

        The structural upper bound for SCP-BiD, exactly as `reachable_from`
        is for SCP-GNN. On the real graph this set is around six times larger
        (396 pairs against 66 across the 22 incidents), which is the price of
        the recall it unlocks - see docs/ml/direction-study.md.
        """
        if self.bi_src is None or self.bi_dst is None:
            return self.reachable_from(root_index)

        seen = np.zeros(self.num_nodes, dtype=bool)
        seen[root_index] = True
        adj: dict[int, list[int]] = {}
        for e in range(self.bi_src.shape[0]):
            adj.setdefault(int(self.bi_src[e]), []).append(int(self.bi_dst[e]))

        frontier = [root_index]
        while frontier:
            node = frontier.pop()
            for nxt in adj.get(node, ()):
                if not seen[nxt]:
                    seen[nxt] = True
                    frontier.append(nxt)

        seen[root_index] = False
        return seen

    def hop_distance_from(self, root_index: int) -> np.ndarray:
        """Shortest propagation-hop count from root; -1 where unreachable."""
        dist = np.full(self.num_nodes, -1, dtype=np.int64)
        dist[root_index] = 0
        adj: dict[int, list[int]] = {}
        for e in range(self.num_edges):
            adj.setdefault(int(self.src[e]), []).append(int(self.dst[e]))

        frontier = [root_index]
        while frontier:
            nxt_frontier = []
            for node in frontier:
                for nbr in adj.get(node, ()):
                    if dist[nbr] == -1:
                        dist[nbr] = dist[node] + 1
                        nxt_frontier.append(nbr)
            frontier = nxt_frontier
        return dist


def from_edges(
    components: list[dict],
    dependencies: list[dict],
) -> Graph:
    """
    Build a Graph from component and dependency records.

    Parameters
    ----------
    components:
        Dicts with keys: component_id, component_name, component_type,
        criticality, is_active.
    dependencies:
        Dicts with keys: component_id, depends_on_id, dependency_type,
        is_critical. These are DEPENDENCY edges; they are reversed here into
        propagation edges, once, as documented at the top of this module.

    Self-loops and edges touching unknown components are dropped. The
    database forbids both (a trigger rejects self-dependency, foreign keys
    reject dangling ids), so this only matters for synthetic graphs and for
    defensive robustness.
    """
    node_ids = np.array([c["component_id"] for c in components], dtype=np.int64)
    index = {int(cid): i for i, cid in enumerate(node_ids)}
    num_nodes = len(components)

    # Degrees are computed before features so they can be used as features.
    in_deg = np.zeros(num_nodes, dtype=np.float64)   # how many depend on me
    out_deg = np.zeros(num_nodes, dtype=np.float64)  # how many I depend on
    kept: list[dict] = []
    for d in dependencies:
        u, v = int(d["component_id"]), int(d["depends_on_id"])
        if u == v or u not in index or v not in index:
            continue
        kept.append(d)
        in_deg[index[v]] += 1.0
        out_deg[index[u]] += 1.0

    # Degree normalisation: log1p then scale. Raw degree is unbounded and
    # scales with graph size, which would stop a model trained on 40-node
    # graphs from transferring to 200-node ones. log1p compresses the tail;
    # dividing by log1p(N) keeps the value in roughly [0, 1] at any size.
    denom = float(np.log1p(max(num_nodes - 1, 1)))
    in_deg_n = np.log1p(in_deg) / denom
    out_deg_n = np.log1p(out_deg) / denom

    node_features = np.zeros((num_nodes, NODE_FEATURE_DIM), dtype=np.float32)
    names: list[str] = []
    for i, c in enumerate(components):
        row = [
            *_one_hot(str(c.get("component_type", "")), COMPONENT_TYPES),
            CRITICALITY_SCALE.get(str(c.get("criticality", "Medium")), 0.5),
            float(c.get("is_active", 1)),
            float(in_deg_n[i]),
            float(out_deg_n[i]),
        ]
        node_features[i] = np.asarray(row, dtype=np.float32)
        names.append(str(c.get("component_name", f"component_{c['component_id']}")))

    num_edges = len(kept)
    src = np.zeros(num_edges, dtype=np.int64)
    dst = np.zeros(num_edges, dtype=np.int64)
    edge_features = np.zeros((num_edges, EDGE_FEATURE_DIM), dtype=np.float32)
    for e, d in enumerate(kept):
        # The one reversal: failure flows from the dependency to the dependent.
        src[e] = index[int(d["depends_on_id"])]
        dst[e] = index[int(d["component_id"])]
        edge_features[e] = np.asarray(
            [
                *_one_hot(str(d.get("dependency_type", "")), DEPENDENCY_TYPES),
                float(d.get("is_critical", 0)),
            ],
            dtype=np.float32,
        )

    # --- bidirectional view ---------------------------------------------
    # Reverse edges first (so bi_src[:E] == src), then the same dependency
    # rows traversed forwards. The direction flag is the only new feature.
    bi_src = np.concatenate([src, dst])
    bi_dst = np.concatenate([dst, src])
    flags = np.concatenate(
        [
            np.full((num_edges, 1), DIRECTION_REVERSE, dtype=np.float32),
            np.full((num_edges, 1), DIRECTION_FORWARD, dtype=np.float32),
        ]
    )
    bi_edge_features = np.concatenate(
        [np.concatenate([edge_features, edge_features], axis=0), flags], axis=1
    )

    return Graph(
        node_ids=node_ids,
        node_features=node_features,
        src=src,
        dst=dst,
        edge_features=edge_features,
        names=names,
        bi_src=bi_src,
        bi_dst=bi_dst,
        bi_edge_features=bi_edge_features,
    )


@dataclass
class Cascade:
    """
    One observed or simulated failure scenario on a graph.

    root_index:
        Index of the component that failed first (the root cause).
    labels:
        (N,) float32 in {0, 1}. 1 = this component was affected. The root's
        own entry is excluded from training and evaluation by `eval_mask`,
        because "the root cause failed" is an input, not something to predict.
    """

    root_index: int
    labels: np.ndarray

    def eval_mask(
        self,
        graph: Graph,
        reachable_only: bool = True,
        bidirectional: bool = False,
    ) -> np.ndarray:
        """Which nodes count towards loss and metrics.

        Excludes the root always. When `reachable_only`, also excludes nodes
        the root cannot reach: those are structurally guaranteed to be 0 by
        the propagation rule, so including them inflates every metric with
        free true negatives and teaches the model nothing. Ablation
        `--no-reach-mask` turns this off to measure its effect.

        `bidirectional` widens the candidate set to components reachable by
        following dependency edges in either direction. The directionality
        experiment scores EVERY model on that wider set, including the
        reverse-only ones: a reverse-only model simply predicts 0 on the
        components it structurally cannot reach, which is exactly the cost of
        ignoring forward propagation and should show up in its recall.
        Scoring each model on its own reachable set would compare them on
        different questions.
        """
        mask = np.ones(graph.num_nodes, dtype=bool)
        mask[self.root_index] = False
        if reachable_only:
            mask &= (
                graph.reachable_bidirectional(self.root_index)
                if bidirectional
                else graph.reachable_from(self.root_index)
            )
        return mask
