"""Description lint (task 3.6): docs text must not reveal hidden columns.

Removing a hidden column from metadata is pointless if a visible node's description says
"includes each employee's annual_salary". For every principal, every text the principal can
see (node descriptions, caveat text, query-example questions, notes and SQL, visible column
descriptions) is checked for mentions of a column hidden from that principal: its name, its
name with spaces, or its PII kind ("salary", "email", "phone"). `name` is too common to flag
on its own, so name columns are matched by full name only.

A text is checked against the hidden columns of the tables it describes: a relation itself
and its direct lineage neighbours, a column's table, a caveat's or metric's targets, a query
example's tables. "Campaigns (especially email)" on a marketing caveat is about a channel, not
the customers table's email column, so it isn't flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from warehouse_graph.governance.metadata import filter_graph
from warehouse_graph.governance.policies import PolicySpec
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import (
    ColumnNode,
    EdgeType,
    MetricNode,
    ModelNode,
    QueryExampleNode,
    SourceNode,
)

_GENERIC_KINDS = {"name"}


@dataclass(frozen=True)
class LintFinding:
    principal: str
    node_id: str
    hidden_column: str
    term: str
    excerpt: str


def _terms(col: ColumnNode) -> set[str]:
    terms = {col.name.lower(), col.name.lower().replace("_", " ")}
    if col.pii and col.pii not in _GENERIC_KINDS:
        terms.add(col.pii.lower())
    return terms


def _related_relations(graph: GraphArtifact, node_id: str) -> set[str]:
    """Relations a node's text describes (see the module docstring)."""
    g = graph.graph
    node = graph.nodes[node_id]
    if isinstance(node, ColumnNode):
        anchors = {node.parent}
    elif isinstance(node, ModelNode | SourceNode):
        anchors = {node_id}
    else:
        anchors = {v for _, v in g.out_edges(node_id)} | {u for u, _ in g.in_edges(node_id)}
        anchors = {graph.nodes[a].parent if a.startswith("column.") else a for a in anchors}  # type: ignore[union-attr]
        anchors |= {
            m.model
            for a in list(anchors)
            if isinstance(m := graph.nodes[a], MetricNode) and m.model
        }
    related = set(anchors)
    if not isinstance(node, ModelNode | SourceNode):
        return related  # caveats, metrics, examples: only what they're attached to
    for a in anchors:
        for _, v, k in g.out_edges(a, keys=True):
            if k == EdgeType.DEPENDS_ON.value:
                related.add(v)
        for u, _, k in g.in_edges(a, keys=True):
            if k == EdgeType.DEPENDS_ON.value:
                related.add(u)
    return related


def lint_descriptions(spec: PolicySpec, graph: GraphArtifact) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for name, access in sorted(spec.access.items()):
        hidden = [graph.nodes[c] for c in sorted(access.hidden_columns)]
        patterns = [
            (col.id, term, re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE))
            for col in hidden
            if isinstance(col, ColumnNode)
            for term in sorted(_terms(col))
        ]
        if not patterns:
            continue
        visible = filter_graph(graph, access, None)  # embeddings unused here
        seen: set[tuple[str, str]] = set()
        for node in visible.nodes.values():
            texts = [node.description]
            if isinstance(node, QueryExampleNode):
                texts += [node.notes or "", node.sql]
            scope = _related_relations(graph, node.id)
            for text in texts:
                for col_id, term, pattern in patterns:
                    parent = graph.nodes[col_id].parent  # type: ignore[union-attr]
                    if parent not in scope or (node.id, term) in seen:
                        continue
                    if m := pattern.search(text):
                        seen.add((node.id, term))
                        start = max(0, m.start() - 40)
                        findings.append(
                            LintFinding(
                                principal=name,
                                node_id=node.id,
                                hidden_column=col_id,
                                term=term,
                                excerpt=text[start : m.end() + 40].replace("\n", " "),
                            )
                        )
    return findings
