"""Column-level lineage from compiled SQL with sqlglot (task 2.4: best effort, time-boxed).

Each model's compiled SQL is qualified against the catalog schema, and every output column is
traced to the warehouse columns it reads. Those become `COLUMN_DERIVES_FROM` edges.
Models that fail to parse are recorded and skipped, never hand-fixed.

Coverage = share of model columns with at least one upstream column. Columns built only from
literals or functions without column inputs (e.g. the time spine's generated dates) have no
upstream, so 100% is not expected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node as LineageNode
from sqlglot.lineage import lineage

from warehouse_graph.graph.schema import ColumnNode, Edge, EdgeType, ModelNode, Node, column_id

DIALECT = "postgres"


@dataclass
class LineageReport:
    model_columns: int = 0
    columns_with_upstream: int = 0
    failed_models: dict[str, str] = field(default_factory=dict)  # model id -> error
    unresolved_columns: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.columns_with_upstream / self.model_columns if self.model_columns else 0.0


def _leaves(node: LineageNode) -> list[LineageNode]:
    return [n for n in node.walk() if not n.downstream and isinstance(n.source, exp.Table)]


def column_lineage(
    nodes: list[Node],
    schema: dict[str, dict[str, dict[str, dict[str, str]]]],
    relations: dict[tuple[str, str], str],
) -> tuple[list[Edge], LineageReport]:
    known = {n.id for n in nodes}
    columns_of: dict[str, list[str]] = {}
    for n in nodes:
        if isinstance(n, ColumnNode):
            columns_of.setdefault(n.parent, []).append(n.name)

    report = LineageReport()
    edges: set[Edge] = set()
    for model in (n for n in nodes if isinstance(n, ModelNode)):
        own = columns_of.get(model.id, [])
        report.model_columns += len(own)
        try:
            traced = lineage(None, model.compiled_sql, schema=schema, dialect=DIALECT)
        except SqlglotError as e:
            report.failed_models[model.id] = str(e).splitlines()[0]
            report.unresolved_columns += [column_id(model.id, c) for c in own]
            continue

        for col in own:
            target = column_id(model.id, col)
            upstream: set[str] = set()
            if col in traced:
                for leaf in _leaves(traced[col]):
                    table: exp.Table = leaf.source  # type: ignore[assignment]
                    parent = relations.get((table.db.lower(), table.name.lower()))
                    if parent is None:
                        continue
                    source_col = column_id(parent, leaf.name.split(".")[-1])
                    if source_col in known:
                        upstream.add(source_col)
            if upstream:
                report.columns_with_upstream += 1
                edges.update(
                    Edge(source=target, target=u, type=EdgeType.COLUMN_DERIVES_FROM)
                    for u in upstream
                )
            else:
                report.unresolved_columns.append(target)
    return sorted(edges, key=lambda e: (e.source, e.target)), report
