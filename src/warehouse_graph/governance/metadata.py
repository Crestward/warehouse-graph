"""Role-filtered metadata (task 3.5): the graph as one principal is allowed to see it.

Hidden relations and columns are removed entirely, not flagged, so search, lineage and node
records can't confirm they exist ("not found" for a hidden id is the same as for a made-up id).

Removed or changed for a principal:
- models/sources it can't query, and their columns
- hidden columns; masked columns stay, with `masked: true`
- metrics whose model is hidden or whose SQL reads a hidden column
- query examples that read a hidden table or column
- exposures and caveats with no visible node left to attach to
- edges with an endpoint removed
- compiled model SQL, unless the principal sees everything (it names upstream tables and
  every column, hidden ones included)
- embeddings of relations that lost columns are recomputed, because the text they were
  embedded from listed the hidden column names
"""

from __future__ import annotations

import re

import numpy as np
import sqlglot
from sqlglot import exp

from warehouse_graph.governance.policies import Access
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder, node_text
from warehouse_graph.graph.schema import (
    CaveatNode,
    ColumnNode,
    EdgeType,
    ExposureNode,
    MetricNode,
    ModelNode,
    Node,
    QueryExampleNode,
    SourceNode,
)


def _sql_columns(sql: str) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except sqlglot.errors.ParseError:
        return set(re.findall(r"[a-z_][a-z0-9_]*", sql.lower()))  # fail closed: every word
    return {c.name.lower() for c in tree.find_all(exp.Column)}


def filter_graph(
    artifact: GraphArtifact, access: Access, embedder: Embedder | None
) -> GraphArtifact:
    """`embedder=None` skips re-embedding: affected rows are zeroed (unsearchable, never leaky)."""
    nodes = artifact.nodes
    edges = artifact.edges
    hidden_names: dict[str, set[str]] = {}  # relation id -> hidden column names
    for cid in access.hidden_columns:
        col = nodes[cid]
        assert isinstance(col, ColumnNode)
        hidden_names.setdefault(col.parent, set()).add(col.name)
    sees_everything = not access.hidden_columns and len(access.relations) == sum(
        isinstance(n, ModelNode | SourceNode) for n in nodes.values()
    )

    keep: dict[str, Node] = {}
    for nid, n in nodes.items():
        if isinstance(n, SourceNode) and nid in access.relations:
            keep[nid] = n
        elif isinstance(n, ModelNode) and nid in access.relations:
            keep[nid] = n if sees_everything else n.model_copy(update={"compiled_sql": ""})
        elif (
            isinstance(n, ColumnNode)
            and n.parent in access.relations
            and nid not in access.hidden_columns
        ):
            keep[nid] = n.model_copy(update={"masked": nid in access.masked_columns})

    for nid, n in nodes.items():
        if isinstance(n, MetricNode):
            if n.model in keep and not (_sql_columns(n.sql) & hidden_names.get(n.model, set())):
                keep[nid] = n
        elif isinstance(n, QueryExampleNode):
            reads = [e.source for e in edges if e.target == nid and e.type is EdgeType.ANSWERED_BY]
            hidden = set().union(*(hidden_names.get(r, set()) for r in reads))
            if all(r in keep for r in reads) and not (_sql_columns(n.sql) & hidden):
                keep[nid] = n
    for kind, edge_type in ((ExposureNode, EdgeType.EXPOSES), (CaveatNode, EdgeType.HAS_CAVEAT)):
        for nid, n in nodes.items():
            if not isinstance(n, kind):
                continue
            # Exposures point at models; caveats are pointed at by what they describe.
            attached = [
                e.target if kind is ExposureNode else e.source
                for e in edges
                if e.type is edge_type and nid in (e.source, e.target)
            ]
            if any(a in keep for a in attached):
                keep[nid] = n

    kept_edges = [e for e in edges if e.source in keep and e.target in keep]
    rows = [artifact.row[nid] for nid in keep]
    embeddings = artifact.embeddings[rows].copy()

    # Re-embed relations whose text listed columns that are now hidden.
    stale = [i for i, nid in enumerate(keep) if nid in hidden_names]
    if stale and embedder is None:
        embeddings[stale] = 0.0
    elif stale and embedder is not None:
        if embedder.name != artifact.meta.embedding_model:
            raise ValueError(
                f"graph was embedded with {artifact.meta.embedding_model}, not {embedder.name}"
            )
        columns: dict[str, list[str]] = {}
        for n in keep.values():
            if isinstance(n, ColumnNode):
                columns.setdefault(n.parent, []).append(n.name)
        ids = list(keep)
        texts = [node_text(keep[ids[i]], columns.get(ids[i])) for i in stale]
        embeddings[stale] = embedder.embed(texts)

    return GraphArtifact(
        meta=artifact.meta,
        nodes=keep,
        edges=kept_edges,
        embeddings=np.asarray(embeddings, dtype=np.float32),
    )
