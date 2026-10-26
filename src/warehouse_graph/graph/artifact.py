"""The graph artifact (task 2.5): one versioned file holding the graph and its embeddings.

    graph.wgz (a zip)
      meta.json        format version, input hashes, embedding model, stats
      graph.json       nodes (full records) + edges
      embeddings.npy   float32 matrix, one row per node in graph.json order

The file is built by `warehouse-graph build` and loaded into memory by the query API and the
MCP server. It has no database dependency, so it works the same in every profile. No pickle:
loading a graph file can't run code.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
from pydantic import BaseModel, ConfigDict

from warehouse_graph.graph import enrich
from warehouse_graph.graph.dbt import MetadataSource
from warehouse_graph.graph.embed import Embedder, Matrix, node_text
from warehouse_graph.graph.lineage import column_lineage
from warehouse_graph.graph.schema import NODE_ADAPTER, ColumnNode, Edge, EdgeType, Node

FORMAT_VERSION = 1


class GraphStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    nodes_by_type: dict[str, int]
    edges_by_type: dict[str, int]
    model_columns: int
    columns_with_upstream: int
    column_lineage_coverage: float
    lineage_failed_models: dict[str, str]
    unresolved_columns: list[str]
    build_seconds: float


class ArtifactMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_version: int
    warehouse_graph_version: str
    built_at: dt.datetime
    manifest_sha256: str
    inputs_sha256: str
    embedding_model: str
    embedding_dims: int
    stats: GraphStats


@dataclass
class GraphArtifact:
    meta: ArtifactMeta
    nodes: dict[str, Node]  # insertion order = embedding row order
    edges: list[Edge]
    embeddings: Matrix

    def __post_init__(self) -> None:
        self.graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        for node_id, node in self.nodes.items():
            self.graph.add_node(node_id, type=node.type.value)
        for e in self.edges:
            self.graph.add_edge(e.source, e.target, key=e.type.value, type=e.type.value)
        self.row = {node_id: i for i, node_id in enumerate(self.nodes)}

    # -- persistence -------------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        graph = {
            "nodes": [n.model_dump(mode="json") for n in self.nodes.values()],
            "edges": [e.model_dump(mode="json") for e in self.edges],
        }
        buf = io.BytesIO()
        np.save(buf, self.embeddings, allow_pickle=False)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("meta.json", self.meta.model_dump_json(indent=2))
            z.writestr("graph.json", json.dumps(graph))
            z.writestr("embeddings.npy", buf.getvalue())

    @classmethod
    def load(cls, path: Path) -> GraphArtifact:
        with zipfile.ZipFile(path) as z:
            meta = ArtifactMeta.model_validate_json(z.read("meta.json"))
            if meta.format_version != FORMAT_VERSION:
                raise ValueError(
                    f"{path} has graph format {meta.format_version}, expected {FORMAT_VERSION};"
                    " rebuild it with `warehouse-graph build`"
                )
            graph: dict[str, Any] = json.loads(z.read("graph.json"))
            embeddings = np.load(io.BytesIO(z.read("embeddings.npy")), allow_pickle=False)
        nodes = [NODE_ADAPTER.validate_python(n) for n in graph["nodes"]]
        return cls(
            meta=meta,
            nodes={n.id: n for n in nodes},
            edges=[Edge.model_validate(e) for e in graph["edges"]],
            embeddings=embeddings.astype(np.float32),
        )


def build(
    source: MetadataSource,
    embedder: Embedder,
    caveats: Path | None = None,
    query_examples: Path | None = None,
) -> GraphArtifact:
    started = time.perf_counter()
    nodes, edges = source.nodes_and_edges()
    nodes = enrich.resolve_references(nodes)
    relations = source.relations()

    lineage_edges, report = column_lineage(nodes, source.table_schema(), relations)
    edges += lineage_edges
    nodes = enrich.propagate_sensitivity(nodes, edges)
    if caveats is not None:
        more_nodes, more_edges = enrich.load_caveats(caveats, {n.id for n in nodes})
        nodes += more_nodes
        edges += more_edges
    if query_examples is not None:
        more_nodes, more_edges = enrich.load_query_examples(query_examples, relations)
        nodes += more_nodes
        edges += more_edges

    by_id: dict[str, Node] = {}
    for n in nodes:
        if n.id in by_id:
            raise ValueError(f"duplicate node id {n.id}")
        by_id[n.id] = n
    dangling = [e for e in edges if e.source not in by_id or e.target not in by_id]
    if dangling:
        raise ValueError(f"{len(dangling)} edges point at unknown nodes, e.g. {dangling[0]}")

    columns: dict[str, list[str]] = {}
    for n in nodes:
        if isinstance(n, ColumnNode):
            columns.setdefault(n.parent, []).append(n.name)
    embeddings = embedder.embed([node_text(n, columns.get(n.id)) for n in by_id.values()])

    stats = GraphStats(
        nodes_by_type=dict(sorted(Counter(n.type.value for n in nodes).items())),
        edges_by_type={t.value: c for t in EdgeType if (c := sum(1 for e in edges if e.type is t))},
        model_columns=report.model_columns,
        columns_with_upstream=report.columns_with_upstream,
        column_lineage_coverage=round(report.coverage, 4),
        lineage_failed_models=report.failed_models,
        unresolved_columns=report.unresolved_columns,
        build_seconds=round(time.perf_counter() - started, 2),
    )
    meta = ArtifactMeta(
        format_version=FORMAT_VERSION,
        warehouse_graph_version=version("warehouse-graph"),
        built_at=dt.datetime.now(dt.UTC),
        manifest_sha256=source.manifest_sha256(),
        inputs_sha256=source.fingerprint(),
        embedding_model=embedder.name,
        embedding_dims=int(embeddings.shape[1]),
        stats=stats,
    )
    return GraphArtifact(meta=meta, nodes=by_id, edges=edges, embeddings=embeddings)
