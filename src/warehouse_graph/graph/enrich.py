"""Enrichment: the knowledge that isn't in dbt's structure (task 2.3).

- `meta` conventions of the demo project: owner, tags, deprecation + `replaced_by`, traps,
  PII/sensitivity (`node_meta`, `column_meta`, `resolve_references`)
- PII/sensitivity tags spread along pass-through column lineage (`propagate_sensitivity`), so
  undocumented raw columns such as `shop.customers.email` are tagged too
- `caveats.yml` -> Caveat nodes + HAS_CAVEAT edges
- `query_examples.yml` -> QueryExample nodes + ANSWERED_BY edges from the models each
  example's SQL reads
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from warehouse_graph.graph.schema import (
    CaveatNode,
    ColumnNode,
    Edge,
    EdgeType,
    Node,
    QueryExampleNode,
    Trap,
)


def node_meta(meta: dict[str, Any], tags: Iterable[str]) -> dict[str, Any]:
    """Fields every node type takes from dbt `meta` (references resolved later)."""
    trap = meta.get("trap")
    return {
        "owner": meta.get("owner"),
        "tags": tuple(sorted(set(tags))),
        "deprecated": bool(meta.get("deprecated")),
        "replaced_by": meta.get("replaced_by"),
        "trap": Trap(**trap) if trap else None,
    }


def column_meta(meta: dict[str, Any]) -> dict[str, Any]:
    fields = node_meta(meta, meta.get("tags", []))
    fields.pop("owner")  # columns inherit their model's owner
    return {**fields, "pii": meta.get("pii"), "sensitivity": meta.get("sensitivity")}


def resolve_references(nodes: list[Node]) -> list[Node]:
    """Turn `replaced_by: fct_orders` / `dim_customers.segment` into node ids.

    A column that holds PII is also given `sensitivity: pii` when it has no stricter tag, so
    filtering on sensitivity alone never misses a PII column.
    """
    ids = {n.id for n in nodes}

    def resolve(ref: str | None) -> str | None:
        if ref is None or ref in ids:
            return ref
        for candidate in (f"model.{ref}", f"column.{ref}", f"metric.{ref}", f"source.{ref}"):
            if candidate in ids:
                return candidate
        raise ValueError(f"replaced_by '{ref}' does not match any node")

    out: list[Node] = []
    for n in nodes:
        update: dict[str, Any] = {}
        if n.replaced_by and n.replaced_by not in ids:
            update["replaced_by"] = resolve(n.replaced_by)
        if isinstance(n, ColumnNode) and n.pii and not n.sensitivity:
            update["sensitivity"] = "pii"
        out.append(n.model_copy(update=update) if update else n)
    return out


def propagate_sensitivity(nodes: list[Node], edges: list[Edge]) -> list[Node]:
    """Copy `pii`/`sensitivity` across COLUMN_DERIVES_FROM edges between same-named columns.

    A same-named column is a pass-through (`select c.email`), so it holds the same data. Tags
    spread in both directions until nothing changes. Renamed or computed columns are left
    alone: deriving `has_email` from `email` doesn't make it PII.
    """
    columns = {n.id: n for n in nodes if isinstance(n, ColumnNode)}
    links = [
        (e.source, e.target)
        for e in edges
        if e.type is EdgeType.COLUMN_DERIVES_FROM
        and columns[e.source].name == columns[e.target].name
    ]
    tags = {cid: (c.pii, c.sensitivity) for cid, c in columns.items()}
    changed = True
    while changed:
        changed = False
        for a, b in links:
            for src, dst in ((a, b), (b, a)):
                pii, sens = tags[src]
                old_pii, old_sens = tags[dst]
                new = (old_pii or pii, old_sens or sens)
                if new != tags[dst]:
                    tags[dst] = new
                    changed = True
    return [
        n.model_copy(update={"pii": tags[n.id][0], "sensitivity": tags[n.id][1]})
        if isinstance(n, ColumnNode) and tags[n.id] != (n.pii, n.sensitivity)
        else n
        for n in nodes
    ]


def _yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def load_caveats(path: Path, known_ids: set[str]) -> tuple[list[Node], list[Edge]]:
    nodes: list[Node] = []
    edges: list[Edge] = []
    for c in _yaml(path)["caveats"]:
        cid = f"caveat.{c['id']}"
        nodes.append(
            CaveatNode(
                id=cid,
                name=c["id"],
                description=" ".join(c["text"].split()),
                severity=c["severity"],
                owner=c.get("author"),
                author=c.get("author"),
                added_on=c.get("added_on"),
            )
        )
        for ref in c["applies_to"]:
            if ref not in known_ids:
                raise ValueError(f"caveat {c['id']} applies to unknown node {ref}")
            edges.append(Edge(source=ref, target=cid, type=EdgeType.HAS_CAVEAT))
    return nodes, edges


def tables_read(sql: str, dialect: str = "postgres") -> set[tuple[str, str]]:
    """(schema, table) pairs a query reads, CTE names excluded."""
    tree = sqlglot.parse_one(sql, dialect=dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return {
        (t.db.lower(), t.name.lower())
        for t in tree.find_all(exp.Table)
        if t.name.lower() not in ctes
    }


def load_query_examples(
    path: Path, relations: dict[tuple[str, str], str]
) -> tuple[list[Node], list[Edge]]:
    data = _yaml(path)
    nodes: list[Node] = []
    edges: list[Edge] = []
    for q in data["examples"]:
        qid = f"query_example.{q['id']}"
        nodes.append(
            QueryExampleNode(
                id=qid,
                name=q["id"],
                description=q["question"].strip(),
                sql=q["sql"].strip(),
                notes=q.get("notes"),
                owner=q.get("author", data.get("author")),
                author=q.get("author", data.get("author")),
                added_on=q.get("added_on", data.get("added_on")),
            )
        )
        read = tables_read(q["sql"])
        unknown = read - relations.keys()
        if unknown:
            raise ValueError(f"query example {q['id']} reads unknown tables {sorted(unknown)}")
        edges += [
            Edge(source=relations[t], target=qid, type=EdgeType.ANSWERED_BY) for t in sorted(read)
        ]
    return nodes, edges
