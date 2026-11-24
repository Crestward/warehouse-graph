"""dbt artifacts -> graph nodes and edges, behind the `MetadataSource` protocol (PRD §6.3).

Reads the four artifacts `warehouse-graph demo-build` collects:

    manifest.json     models, sources, columns (docs + meta), tests, metrics, exposures
    catalog.json      actual column names and types in the warehouse
    run_results.json  last build status per model
    sources.json      last freshness result per source

dbt-only conventions (`meta.owner`, `meta.trap`, `meta.deprecated`, ...) are interpreted in
`enrich.meta_fields`, so this module only maps dbt's structure.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

from warehouse_graph.graph import enrich
from warehouse_graph.graph.schema import (
    ColumnNode,
    Edge,
    EdgeType,
    ExposureNode,
    MetricNode,
    ModelNode,
    Node,
    SourceNode,
    column_id,
)

ARTIFACTS = ("manifest.json", "catalog.json", "run_results.json", "sources.json")
_HOURS = {"minute": 1 / 60, "hour": 1.0, "day": 24.0}


class MetadataSource(Protocol):
    """Where the graph's structure comes from. dbt artifacts are the only v1 implementation."""

    def fingerprint(self) -> str:
        """sha256 over every input, recorded in the graph artifact."""
        ...

    def manifest_sha256(self) -> str:
        """sha256 of the structural metadata alone (the dbt manifest)."""
        ...

    def nodes_and_edges(self) -> tuple[list[Node], list[Edge]]: ...

    def table_schema(self) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
        """{database: {schema: {table: {column: type}}}}, for SQL parsing."""
        ...

    def relations(self) -> dict[tuple[str, str], str]:
        """(schema, table) -> node id of the model or source stored there."""
        ...


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _hours(rule: dict[str, Any] | None) -> float | None:
    if not rule or rule.get("count") is None:
        return None
    return float(rule["count"]) * _HOURS[rule["period"]]


def _ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def short_id(unique_id: str, node: dict[str, Any]) -> str:
    """dbt unique_id -> graph node id (`model.kiwi_retail.x` -> `model.x`)."""
    kind = node["resource_type"]
    if kind == "source":
        return f"source.{node['source_name']}.{node['name']}"
    if kind in ("model", "metric", "exposure"):
        return f"{kind}.{node['name']}"
    raise ValueError(f"unsupported dbt resource {unique_id}")


class DbtArtifacts:
    """`MetadataSource` over a directory holding the four dbt artifacts."""

    def __init__(self, artifacts_dir: Path) -> None:
        missing = [n for n in ARTIFACTS if not (artifacts_dir / n).exists()]
        if missing:
            raise FileNotFoundError(
                f"{artifacts_dir} is missing {', '.join(missing)}; run `make warehouse`"
            )
        self.dir = artifacts_dir
        self.manifest = _load(artifacts_dir / "manifest.json")
        self.catalog = _load(artifacts_dir / "catalog.json")
        self.run_results = _load(artifacts_dir / "run_results.json")
        self.sources_json = _load(artifacts_dir / "sources.json")
        self._ids: dict[str, str] = {}  # dbt unique_id -> node id
        for uid, n in [*self.manifest["nodes"].items(), *self.manifest["sources"].items()]:
            if n["resource_type"] in ("model", "source"):
                self._ids[uid] = short_id(uid, n)
        for section in ("metrics", "exposures"):
            for uid, n in self.manifest[section].items():
                self._ids[uid] = short_id(uid, n)

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for name in ARTIFACTS:
            h.update((self.dir / name).read_bytes())
        return h.hexdigest()

    def manifest_sha256(self) -> str:
        return hashlib.sha256((self.dir / "manifest.json").read_bytes()).hexdigest()

    # -- structure ---------------------------------------------------------------------------

    def _models(self) -> dict[str, dict[str, Any]]:
        return {
            uid: n for uid, n in self.manifest["nodes"].items() if n["resource_type"] == "model"
        }

    def table_schema(self) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
        schema: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
        for entry in [*self.catalog["nodes"].values(), *self.catalog["sources"].values()]:
            md = entry["metadata"]
            schema.setdefault(md["database"], {}).setdefault(md["schema"], {})[md["name"]] = {
                c: v["type"] for c, v in entry["columns"].items()
            }
        return schema

    def relations(self) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for uid, n in [*self._models().items(), *self.manifest["sources"].items()]:
            table = n.get("alias") or n.get("identifier") or n["name"]
            out[(n["schema"].lower(), table.lower())] = self._ids[uid]
        return out

    def _column_tests(self) -> dict[tuple[str, str], list[str]]:
        tests: dict[tuple[str, str], list[str]] = defaultdict(list)
        for n in self.manifest["nodes"].values():
            meta = n.get("test_metadata")
            if n["resource_type"] == "test" and meta and n.get("column_name"):
                tests[(n["attached_node"], n["column_name"])].append(meta["name"])
        return tests

    def _last_runs(self) -> dict[str, tuple[str, dt.datetime | None]]:
        runs: dict[str, tuple[str, dt.datetime | None]] = {}
        for r in self.run_results["results"]:
            done = [t["completed_at"] for t in r["timing"] if t["name"] == "execute"]
            runs[r["unique_id"]] = (r["status"], _ts(done[0]) if done else None)
        return runs

    def _columns(
        self, uid: str, parent: str, node: dict[str, Any], catalog: dict[str, Any] | None
    ) -> tuple[list[Node], list[Edge]]:
        documented: dict[str, Any] = node.get("columns", {})
        actual: dict[str, Any] = catalog["columns"] if catalog else {}
        tests = self._column_tests()
        nodes: list[Node] = []
        edges: list[Edge] = []
        # Catalog order (the warehouse's), then any documented-only columns.
        for name in [*actual, *(c for c in documented if c not in actual)]:
            doc = documented.get(name, {})
            meta = doc.get("config", {}).get("meta") or doc.get("meta") or {}
            cid = column_id(parent, name)
            nodes.append(
                ColumnNode(
                    id=cid,
                    name=name,
                    parent=parent,
                    description=(doc.get("description") or "").strip(),
                    data_type=actual.get(name, {}).get("type") or doc.get("data_type"),
                    tests=tuple(sorted(tests.get((uid, name), []))),
                    **enrich.column_meta(meta),
                )
            )
            edges.append(Edge(source=parent, target=cid, type=EdgeType.HAS_COLUMN))
        return nodes, edges

    def nodes_and_edges(self) -> tuple[list[Node], list[Edge]]:
        nodes: list[Node] = []
        edges: list[Edge] = []
        runs = self._last_runs()
        freshness = {r["unique_id"]: r for r in self.sources_json["results"]}

        for uid, s in self.manifest["sources"].items():
            sid = self._ids[uid]
            config = s.get("config", {})
            meta = config.get("meta") or s.get("meta") or {}
            rules = config.get("freshness") or s.get("freshness") or {}
            fresh = freshness.get(uid, {})
            nodes.append(
                SourceNode(
                    id=sid,
                    name=f"{s['source_name']}.{s['name']}",
                    source_name=s["source_name"],
                    description=(s.get("description") or "").strip(),
                    relation=s["relation_name"],
                    loader=meta.get("loader") or s.get("loader") or None,
                    loaded_at_field=config.get("loaded_at_field") or s.get("loaded_at_field"),
                    freshness_warn_hours=_hours(rules.get("warn_after")),
                    freshness_error_hours=_hours(rules.get("error_after")),
                    stale_on_demand=bool(meta.get("stale_on_demand")),
                    sensitivity=meta.get("sensitivity"),
                    freshness_status=fresh.get("status"),
                    max_loaded_at=_ts(fresh.get("max_loaded_at")),
                    **enrich.node_meta(meta, s.get("tags", [])),
                )
            )
            cols, col_edges = self._columns(uid, sid, s, self.catalog["sources"].get(uid))
            nodes += cols
            edges += col_edges

        for uid, m in self._models().items():
            mid = self._ids[uid]
            meta = m["config"].get("meta") or {}
            status, finished = runs.get(uid, (None, None))
            nodes.append(
                ModelNode(
                    id=mid,
                    name=m["name"],
                    description=(m.get("description") or "").strip(),
                    layer=meta.get("layer"),
                    materialization=m["config"].get("materialized"),
                    relation=m["relation_name"],
                    contains_pii=bool(meta.get("contains_pii")),
                    sensitivity=meta.get("sensitivity"),
                    last_run_status=status,
                    last_successful_run=finished if status == "success" else None,
                    compiled_sql=(m.get("compiled_code") or "").strip(),
                    **enrich.node_meta(meta, m["config"].get("tags", [])),
                )
            )
            edges += [
                Edge(source=mid, target=self._ids[dep], type=EdgeType.DEPENDS_ON)
                for dep in m["depends_on"]["nodes"]
                if dep in self._ids
            ]
            cols, col_edges = self._columns(uid, mid, m, self.catalog["nodes"].get(uid))
            nodes += cols
            edges += col_edges

        for uid, e in self.manifest["exposures"].items():
            eid = self._ids[uid]
            meta = e.get("config", {}).get("meta") or e.get("meta") or {}
            owner = e.get("owner") or {}
            nodes.append(
                ExposureNode(
                    id=eid,
                    name=e["name"],
                    label=e.get("label") or "",
                    description=(e.get("description") or "").strip(),
                    exposure_type=e.get("type") or "",
                    criticality=meta.get("criticality"),
                    maturity=e.get("maturity"),
                    url=e.get("url"),
                    **enrich.node_meta({"owner": owner.get("email"), **meta}, e.get("tags", [])),
                )
            )
            edges += [
                Edge(source=eid, target=self._ids[dep], type=EdgeType.EXPOSES)
                for dep in e["depends_on"]["nodes"]
                if dep in self._ids
            ]

        metric_nodes, metric_edges = self._metrics()
        return nodes + metric_nodes, edges + metric_edges

    # -- metrics -----------------------------------------------------------------------------

    def _measures(self) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        """measure name -> (measure, semantic model)."""
        out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for sm in self.manifest["semantic_models"].values():
            for measure in sm["measures"]:
                out[measure["name"]] = (measure, sm)
        return out

    def _metrics(self) -> tuple[list[Node], list[Edge]]:
        measures = self._measures()
        relation_ids = {n["relation_name"]: self._ids[uid] for uid, n in self._models().items()}
        by_name = {m["name"]: m for m in self.manifest["metrics"].values()}
        nodes: list[Node] = []
        edges: list[Edge] = []

        def simple(metric: dict[str, Any]) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
            measure, sm = measures[metric["type_params"]["measure"]["name"]]
            agg = "count" if measure["agg"] == "count" else measure["agg"]
            expr = f"{agg}({measure.get('expr') or measure['name']})"
            return expr, sm["node_relation"]["relation_name"], measure, sm

        for m in by_name.values():
            meta = m.get("config", {}).get("meta") or m.get("meta") or {}
            params = m["type_params"]
            filters = tuple(
                f["where_sql_template"] for f in (m.get("filter") or {}).get("where_filters", [])
            )
            if m["type"] == "simple":
                expr, relation, _, sm = simple(m)
                where = f"\nwhere {' and '.join(filters)}" if filters else ""
                sql = f"select {expr} as {m['name']}\nfrom {relation}{where}"
                expression = expr
            elif m["type"] == "ratio":
                num_expr, relation, _, sm = simple(by_name[params["numerator"]["name"]])
                den_expr, den_relation, _, _ = simple(by_name[params["denominator"]["name"]])
                if den_relation != relation:
                    raise ValueError(f"ratio metric {m['name']} spans two relations")
                sql = (
                    f"select cast({num_expr} as numeric) / nullif({den_expr}, 0) as {m['name']}"
                    f"\nfrom {relation}"
                )
                expression = (
                    f"metric.{params['numerator']['name']} / metric.{params['denominator']['name']}"
                )
            else:
                raise ValueError(f"metric type {m['type']} is not used by the demo project")

            model_id = relation_ids.get(relation)
            time_dim = sm["defaults"]["agg_time_dimension"] if sm.get("defaults") else None
            grains = {
                d["name"]: (d.get("type_params") or {}).get("time_granularity")
                for d in sm["dimensions"]
                if d["type"] == "time"
            }
            mid = f"metric.{m['name']}"
            superseded = meta.get("superseded_by")
            nodes.append(
                MetricNode(
                    id=mid,
                    name=m["name"],
                    label=m.get("label") or "",
                    description=(m.get("description") or "").strip(),
                    metric_type=m["type"],
                    canonical=bool(meta.get("canonical")),
                    helper=bool(meta.get("helper")),
                    superseded_by=f"metric.{superseded}" if superseded else None,
                    team=meta.get("team"),
                    model=model_id,
                    time_dimension=time_dim,
                    grain=grains.get(time_dim) if time_dim else None,
                    filters=filters,
                    expression=expression,
                    sql=sql,
                    **enrich.node_meta(meta, m.get("tags", [])),
                )
            )
            if model_id:
                edges.append(Edge(source=mid, target=model_id, type=EdgeType.DEFINES))
            edges += [
                Edge(source=mid, target=self._ids[dep], type=EdgeType.DEFINES)
                for dep in m["depends_on"]["nodes"]
                if dep in self._ids
            ]
        return nodes, edges
