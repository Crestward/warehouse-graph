"""Query API over a loaded graph artifact (task 2.6). The MCP tools (Phase 3) are thin
wrappers around these five functions, so their semantics live here:

    search_nodes      hybrid ranking: BM25 + embedding cosine, fused by reciprocal rank
    get_node          full record, with caveats, columns and direct neighbours
    get_lineage       up/downstream subgraph at model or column level
    get_blast_radius  everything downstream, ranked by exposure criticality
    get_metric        definition + canonical SQL (+ the canonical alternative for a trap)
"""

from __future__ import annotations

import math
from collections import Counter, deque
from collections.abc import Iterable
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder, get_embedder, node_text, tokenize
from warehouse_graph.graph.schema import (
    CRITICALITY_RANK,
    CaveatNode,
    ColumnNode,
    Criticality,
    Edge,
    EdgeType,
    ExposureNode,
    MetricNode,
    ModelNode,
    Node,
    NodeType,
    SourceNode,
)

Direction = Literal["upstream", "downstream", "both"]
SearchMode = Literal["hybrid", "vector", "bm25"]
Level = Literal["model", "column"]

RRF_K = 60  # standard reciprocal-rank-fusion constant
CANDIDATES = 100  # per ranker, before fusion
_MODEL_LEVEL = (EdgeType.DEPENDS_ON, EdgeType.EXPOSES, EdgeType.DEFINES)


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True)


class NodeSummary(_Result):
    id: str
    type: NodeType
    name: str
    description: str
    deprecated: bool = False
    trap_kind: str | None = None


class SearchHit(NodeSummary):
    score: float
    bm25_rank: int | None
    vector_rank: int | None


class NodeRecord(_Result):
    node: dict[str, Any]
    caveats: list[dict[str, Any]]
    columns: list[NodeSummary]
    upstream: list[str]
    downstream: list[str]


class Subgraph(_Result):
    root: str
    direction: Direction
    level: Level
    depth: int
    nodes: list[NodeSummary]
    edges: list[Edge]


class Impact(_Result):
    id: str
    type: NodeType
    name: str
    distance: int
    criticality: Criticality | None  # highest criticality of the exposures it feeds


class BlastRadius(_Result):
    root: str
    max_criticality: Criticality | None
    exposures: list[Impact]
    models: list[Impact]
    metrics: list[Impact]


class MetricRecord(_Result):
    metric: dict[str, Any]
    sql: str
    model: str | None
    inputs: list[str]  # ratio metrics: numerator, denominator
    caveats: list[dict[str, Any]]
    canonical_alternative: str | None  # set when this metric is not the canonical definition


def _summary(n: Node) -> NodeSummary:
    return NodeSummary(
        id=n.id,
        type=n.type,
        name=n.name,
        description=n.description[:300],
        deprecated=n.deprecated,
        trap_kind=n.trap.kind if n.trap else None,
    )


class BM25:
    """Okapi BM25 over pre-tokenised documents (k1=1.5, b=0.75)."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = np.array([len(d) for d in docs], dtype=np.float64)
        self.avg = float(self.len.mean()) if docs else 0.0
        df: Counter[str] = Counter()
        for d in self.tf:
            df.update(d.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> np.ndarray[Any, np.dtype[np.float64]]:
        out = np.zeros(len(self.tf))
        norm = self.k1 * (1 - self.b + self.b * self.len / self.avg)
        for term in set(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            f = np.array([d.get(term, 0) for d in self.tf], dtype=np.float64)
            out += idf * f * (self.k1 + 1) / (f + norm)
        return out


class GraphQuery:
    def __init__(self, artifact: GraphArtifact, embedder: Embedder | None = None) -> None:
        self.a = artifact
        self.g = artifact.graph
        self.nodes = artifact.nodes
        self._ids = list(artifact.nodes)
        self._embedder = embedder
        columns: dict[str, list[str]] = {}
        for n in self.nodes.values():
            if isinstance(n, ColumnNode):
                columns.setdefault(n.parent, []).append(n.name)
        self._bm25 = BM25([tokenize(node_text(n, columns.get(n.id))) for n in self.nodes.values()])

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = get_embedder(self.a.meta.embedding_model)
        return self._embedder

    # -- helpers -----------------------------------------------------------------------------

    def _get(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise KeyError(f"unknown node id '{node_id}'; use search_nodes to find ids") from None

    def _out(self, node_id: str, *types: EdgeType) -> list[str]:
        wanted = {t.value for t in types}
        return sorted({v for _, v, k in self.g.out_edges(node_id, keys=True) if k in wanted})

    def _in(self, node_id: str, *types: EdgeType) -> list[str]:
        wanted = {t.value for t in types}
        return sorted({u for u, _, k in self.g.in_edges(node_id, keys=True) if k in wanted})

    def _caveats(self, node_id: str) -> list[dict[str, Any]]:
        caveats = [self.nodes[c] for c in self._out(node_id, EdgeType.HAS_CAVEAT)]
        order = {"high": 0, "medium": 1, "low": 2}
        caveats.sort(key=lambda c: order[c.severity] if isinstance(c, CaveatNode) else 3)
        return [c.model_dump(mode="json") for c in caveats]

    def _bfs(self, starts: Iterable[str], step: Any, depth: int | None) -> dict[str, int]:
        """Breadth-first distances from `starts` (distance 0) following `step(node)`."""
        dist = dict.fromkeys(starts, 0)
        queue = deque(dist)
        while queue:
            node = queue.popleft()
            if depth is not None and dist[node] >= depth:
                continue
            for nxt in step(node):
                if nxt not in dist:
                    dist[nxt] = dist[node] + 1
                    queue.append(nxt)
        return dist

    # -- the five operations -----------------------------------------------------------------

    def search_nodes(
        self,
        query: str,
        types: Iterable[NodeType | str] | None = None,
        k: int = 10,
        mode: SearchMode = "hybrid",
    ) -> list[SearchHit]:
        """`hybrid` fuses the BM25 and vector rankings (RRF); `vector` and `bm25` rank by one
        signal alone, which is what single-signal baselines (P1 retriever 2) use."""
        allowed = {NodeType(t) for t in types} if types else set(NodeType)
        mask = np.array([self.nodes[i].type in allowed for i in self._ids])

        bm25 = np.where(mask, self._bm25.scores(tokenize(query)), -np.inf)
        vector = np.where(mask, self.a.embeddings @ self.embedder.embed([query])[0], -np.inf)
        ranks: list[dict[int, int]] = []
        for scores, floor in ((bm25, 0.0), (vector, -1.0)):
            order = np.argsort(-scores, kind="stable")[:CANDIDATES]
            ranks.append({int(i): r for r, i in enumerate(order, 1) if scores[i] > floor})
        fused: dict[int, float] = {}
        if mode == "hybrid":
            for rank in ranks:
                for i, r in rank.items():
                    fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r)
        else:
            scores = vector if mode == "vector" else bm25
            only = ranks[1] if mode == "vector" else ranks[0]
            fused = {i: float(scores[i]) for i in only}

        best = sorted(fused, key=lambda i: (-fused[i], self._ids[i]))[:k]
        return [
            SearchHit(
                **_summary(self.nodes[self._ids[i]]).model_dump(),
                score=round(fused[i], 6),
                bm25_rank=ranks[0].get(i),
                vector_rank=ranks[1].get(i),
            )
            for i in best
        ]

    def get_node(self, node_id: str) -> NodeRecord:
        node = self._get(node_id)
        dump = node.model_dump(mode="json")
        columns: list[NodeSummary] = []
        if isinstance(node, ModelNode | SourceNode):
            columns = [
                _summary(self.nodes[c])
                for _, c, k in self.g.out_edges(node_id, keys=True)
                if k == EdgeType.HAS_COLUMN.value
            ]
        if isinstance(node, ColumnNode):
            up = self._out(node_id, EdgeType.COLUMN_DERIVES_FROM)
            down = self._in(node_id, EdgeType.COLUMN_DERIVES_FROM)
        else:
            up = self._out(node_id, *_MODEL_LEVEL)
            down = self._in(node_id, *_MODEL_LEVEL)
        return NodeRecord(
            node=dump, caveats=self._caveats(node_id), columns=columns, upstream=up, downstream=down
        )

    def get_lineage(
        self,
        node_id: str,
        direction: Direction = "upstream",
        depth: int = 3,
        level: Level = "model",
    ) -> Subgraph:
        node = self._get(node_id)
        if level == "model":
            types: tuple[EdgeType, ...] = _MODEL_LEVEL
            starts = [node.parent] if isinstance(node, ColumnNode) else [node_id]
        else:
            types = (EdgeType.COLUMN_DERIVES_FROM,)
            starts = (
                [node_id]
                if isinstance(node, ColumnNode)
                else self._out(node_id, EdgeType.HAS_COLUMN)
            )
        if not starts:
            raise ValueError(f"{node_id} has no {level}-level lineage")

        reached: dict[str, int] = {}
        if direction in ("upstream", "both"):
            reached |= self._bfs(starts, lambda n: self._out(n, *types), depth)
        if direction in ("downstream", "both"):
            reached |= self._bfs(starts, lambda n: self._in(n, *types), depth)
        wanted = {t.value for t in types}
        edges = [
            Edge(source=u, target=v, type=EdgeType(key))
            for u, v, key in self.g.subgraph(reached).edges(keys=True)
            if key in wanted
        ]
        return Subgraph(
            root=node_id,
            direction=direction,
            level=level,
            depth=depth,
            nodes=[_summary(self.nodes[n]) for n in sorted(reached, key=lambda n: (reached[n], n))],
            edges=sorted(edges, key=lambda e: (e.source, e.target, e.type.value)),
        )

    def _criticality(self, node_id: str) -> Criticality | None:
        """Highest criticality among the exposures `node_id` feeds (or is)."""
        downstream = self._bfs([node_id], lambda n: self._in(n, *_MODEL_LEVEL), None)
        levels = [n.criticality for d in downstream if isinstance(n := self.nodes[d], ExposureNode)]
        return max(levels, key=lambda c: CRITICALITY_RANK[c], default=None)

    def get_blast_radius(self, node_id: str) -> BlastRadius:
        node = self._get(node_id)
        if isinstance(node, ColumnNode):
            # Columns that derive from this one, then the models that hold them.
            cols = self._bfs([node_id], lambda n: self._in(n, EdgeType.COLUMN_DERIVES_FROM), None)
            starts = {self.nodes[c].parent: d for c, d in cols.items()}  # type: ignore[union-attr]
        else:
            starts = {node_id: 0}

        reached: dict[str, int] = {}
        for start, offset in starts.items():
            for n, d in self._bfs([start], lambda n: self._in(n, *_MODEL_LEVEL), None).items():
                reached[n] = min(reached.get(n, d + offset), d + offset)
        reached.pop(node_id, None)
        if isinstance(node, ColumnNode):
            reached.pop(node.parent, None)

        def impacts(kind: type[Node]) -> list[Impact]:
            out = [
                Impact(
                    id=n,
                    type=self.nodes[n].type,
                    name=self.nodes[n].name,
                    distance=d,
                    criticality=self._criticality(n),
                )
                for n, d in reached.items()
                if isinstance(self.nodes[n], kind)
            ]
            return sorted(out, key=lambda i: (-CRITICALITY_RANK[i.criticality], i.distance, i.id))

        exposures = impacts(ExposureNode)
        return BlastRadius(
            root=node_id,
            max_criticality=exposures[0].criticality if exposures else None,
            exposures=exposures,
            models=impacts(ModelNode),
            metrics=impacts(MetricNode),
        )

    def get_metric(self, name: str) -> MetricRecord:
        metric = self._get(name if name.startswith("metric.") else f"metric.{name}")
        if not isinstance(metric, MetricNode):
            raise KeyError(f"'{name}' is not a metric")
        # Ratio expressions read "metric.numerator / metric.denominator".
        inputs = (
            [part.strip() for part in metric.expression.split("/")]
            if metric.metric_type == "ratio"
            else []
        )
        return MetricRecord(
            metric=metric.model_dump(mode="json"),
            sql=metric.sql,
            model=metric.model,
            inputs=inputs,
            caveats=self._caveats(metric.id),
            canonical_alternative=None if metric.canonical else metric.superseded_by,
        )
