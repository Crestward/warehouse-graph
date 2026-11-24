"""Unit tests for the graph core on the mini dbt fixture (tests/fixtures/dbt_mini).

No database and no model download: embeddings come from `HashEmbedder`.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from warehouse_graph.cli import main
from warehouse_graph.graph import DbtArtifacts, GraphArtifact, GraphConfig, GraphQuery, build
from warehouse_graph.graph.embed import HashEmbedder, node_text, tokenize
from warehouse_graph.graph.enrich import load_caveats, load_query_examples, tables_read
from warehouse_graph.graph.render import mermaid, stats_markdown
from warehouse_graph.graph.schema import (
    ColumnNode,
    EdgeType,
    MetricNode,
    ModelNode,
    NodeType,
    SourceNode,
    column_id,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dbt_mini"


@pytest.fixture(scope="module")
def source() -> DbtArtifacts:
    return DbtArtifacts(FIXTURE)


@pytest.fixture(scope="module")
def artifact(source: DbtArtifacts) -> GraphArtifact:
    return build(
        source,
        HashEmbedder(),
        caveats=FIXTURE / "caveats.yml",
        query_examples=FIXTURE / "query_examples.yml",
    )


@pytest.fixture(scope="module")
def q(artifact: GraphArtifact) -> GraphQuery:
    return GraphQuery(artifact, embedder=HashEmbedder())


def _edges(artifact: GraphArtifact, kind: EdgeType) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in artifact.edges if e.type is kind}


# -- parsing (2.2) ------------------------------------------------------------------------------


def test_missing_artifacts_are_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"manifest\.json"):
        DbtArtifacts(tmp_path)


def test_node_counts(artifact: GraphArtifact) -> None:
    counts = artifact.meta.stats.nodes_by_type
    assert counts["source"] == 5
    assert counts["model"] == 9
    assert counts["exposure"] == 1
    assert counts["metric"] == 1
    assert counts["caveat"] == 2
    assert counts["query_example"] == 2


def test_ids_are_short_and_readable(artifact: GraphArtifact) -> None:
    assert "model.fct_ad_spend_daily" in artifact.nodes
    assert "source.marketing.ad_spend" in artifact.nodes
    assert "column.fct_ad_spend_daily.spend_nzd" in artifact.nodes
    assert "column.marketing.ad_spend.spend" in artifact.nodes
    assert column_id("source.shop.orders", "order_id") == "column.shop.orders.order_id"


def test_model_dependencies(artifact: GraphArtifact) -> None:
    deps = _edges(artifact, EdgeType.DEPENDS_ON)
    assert ("model.orders_v1", "model.stg_shop__orders") in deps
    assert ("model.stg_shop__orders", "source.shop.orders") in deps
    assert ("model.fct_ad_spend_daily", "model.int_marketing__spend_daily") in deps


def test_exposure_and_metric_edges(artifact: GraphArtifact) -> None:
    assert _edges(artifact, EdgeType.EXPOSES) == {
        ("exposure.marketing_performance_dashboard", "model.fct_ad_spend_daily")
    }
    assert ("metric.ad_spend_nzd", "model.fct_ad_spend_daily") in _edges(artifact, EdgeType.DEFINES)


def test_columns_take_types_from_catalog_and_tests_from_manifest(artifact: GraphArtifact) -> None:
    col = artifact.nodes["column.stg_shop__orders.order_id"]
    assert isinstance(col, ColumnNode)
    assert col.parent == "model.stg_shop__orders"
    assert col.data_type == "integer"
    assert {"not_null", "unique"} <= set(col.tests)


def test_model_attributes(artifact: GraphArtifact) -> None:
    m = artifact.nodes["model.fct_ad_spend_daily"]
    assert isinstance(m, ModelNode)
    assert (m.layer, m.materialization) == ("marts", "table")
    assert m.owner == "marketing-analytics@kiwiretail.example"
    assert "marketing" in m.tags
    assert m.last_run_status == "success" and m.last_successful_run is not None
    assert m.compiled_sql.lower().startswith(("with", "select"))


def test_source_freshness(artifact: GraphArtifact) -> None:
    s = artifact.nodes["source.marketing.ad_spend"]
    assert isinstance(s, SourceNode)
    assert (s.freshness_warn_hours, s.freshness_error_hours) == (24.0, 48.0)
    assert s.stale_on_demand
    assert s.freshness_status in {"pass", "warn", "error"}
    assert s.loader == "adtech-vendor-sftp"


def test_metric_definition_and_sql(artifact: GraphArtifact) -> None:
    m = artifact.nodes["metric.ad_spend_nzd"]
    assert isinstance(m, MetricNode)
    assert m.canonical and m.metric_type == "simple"
    assert m.expression == "sum(spend_nzd)"
    assert (m.time_dimension, m.grain) == ("spend_date", "day")
    assert (
        m.sql
        == 'select sum(spend_nzd) as ad_spend_nzd\nfrom "warehouse"."marts"."fct_ad_spend_daily"'
    )


# -- enrichment (2.3) ---------------------------------------------------------------------------


def test_deprecation_resolves_to_node_ids(artifact: GraphArtifact) -> None:
    v1 = artifact.nodes["model.orders_v1"]
    assert v1.deprecated and v1.replaced_by == "model.fct_orders"
    assert v1.trap is not None and v1.trap.kind == "deprecated_model"


def test_pii_and_sensitivity(artifact: GraphArtifact) -> None:
    salary = artifact.nodes["column.stg_hr__employees.annual_salary"]
    assert isinstance(salary, ColumnNode)
    assert (salary.pii, salary.sensitivity) == ("salary", "restricted")
    email = artifact.nodes["column.stg_hr__employees.work_email"]
    assert isinstance(email, ColumnNode)
    assert (email.pii, email.sensitivity) == ("email", "pii")
    hr = artifact.nodes["source.hr.employees"]
    assert isinstance(hr, SourceNode) and hr.sensitivity == "restricted"


def test_pii_spreads_to_undocumented_raw_columns(artifact: GraphArtifact) -> None:
    raw = artifact.nodes["column.hr.employees.annual_salary"]
    assert isinstance(raw, ColumnNode)
    assert (raw.pii, raw.sensitivity) == ("salary", "restricted")
    # A renamed/computed column is not tagged just because it derives from a PII column.
    tagged = {n.id for n in artifact.nodes.values() if isinstance(n, ColumnNode) and n.pii}
    assert all(
        n.split(".")[-1] in {"first_name", "last_name", "work_email", "annual_salary"}
        for n in tagged
    )


def test_caveats_attach_to_nodes(artifact: GraphArtifact) -> None:
    assert _edges(artifact, EdgeType.HAS_CAVEAT) == {
        ("source.marketing.ad_spend", "caveat.cav_spend_delay"),
        ("model.fct_ad_spend_daily", "caveat.cav_spend_delay"),
        ("metric.ad_spend_nzd", "caveat.cav_spend_delay"),
        ("model.orders_v1", "caveat.cav_orders_v1"),
    }


def test_caveat_with_unknown_target_fails(tmp_path: Path) -> None:
    bad = tmp_path / "caveats.yml"
    bad.write_text(
        "caveats:\n  - {id: x, applies_to: [model.nope], severity: low, text: t}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"model\.nope"):
        load_caveats(bad, {"model.real"})


def test_query_examples_link_to_models_they_read(artifact: GraphArtifact) -> None:
    assert _edges(artifact, EdgeType.ANSWERED_BY) == {
        ("model.fct_ad_spend_daily", "query_example.qe01"),
        ("model.fct_orders", "query_example.qe02"),
    }
    qe = artifact.nodes["query_example.qe02"]
    assert qe.owner == "someone-else@kiwiretail.example"  # per-example override
    assert artifact.nodes["query_example.qe01"].owner == "analytics-engineering@kiwiretail.example"


def test_query_example_reading_unknown_table_fails(tmp_path: Path) -> None:
    bad = tmp_path / "qe.yml"
    bad.write_text(
        "examples:\n  - {id: q, question: q, sql: select 1 from marts.nope}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="nope"):
        load_query_examples(bad, {("marts", "real"): "model.real"})


def test_tables_read_skips_ctes() -> None:
    sql = "with a as (select * from marts.x) select * from a join staging.y on true"
    assert tables_read(sql) == {("marts", "x"), ("staging", "y")}


# -- column lineage (2.4) -----------------------------------------------------------------------


def test_column_lineage_traces_through_staging(artifact: GraphArtifact) -> None:
    derives = _edges(artifact, EdgeType.COLUMN_DERIVES_FROM)
    assert (
        "column.stg_marketing__ad_spend.campaign_id",
        "column.marketing.ad_spend.campaign_id",
    ) in derives
    # Every lineage edge stays inside the graph's columns.
    assert all(s.startswith("column.") and t.startswith("column.") for s, t in derives)


def test_lineage_coverage_is_recorded(artifact: GraphArtifact) -> None:
    stats = artifact.meta.stats
    assert stats.model_columns > 0
    assert stats.columns_with_upstream + len(stats.unresolved_columns) == stats.model_columns
    # fct_orders' upstream models are cut from the fixture, so its columns can't be traced.
    assert 0.5 < stats.column_lineage_coverage < 1.0
    assert "column.fct_orders.units" in stats.unresolved_columns


# -- artifact (2.5) -----------------------------------------------------------------------------


def test_artifact_round_trip(artifact: GraphArtifact, tmp_path: Path) -> None:
    path = tmp_path / "graph.wgz"
    artifact.save(path)
    loaded = GraphArtifact.load(path)
    assert loaded.nodes == artifact.nodes
    assert loaded.edges == artifact.edges
    assert (loaded.embeddings == artifact.embeddings).all()
    assert loaded.meta == artifact.meta
    assert loaded.graph.number_of_edges() == len(artifact.edges)


def test_artifact_records_versions_and_hashes(artifact: GraphArtifact) -> None:
    meta = artifact.meta
    assert meta.format_version == 1
    assert len(meta.manifest_sha256) == 64 and len(meta.inputs_sha256) == 64
    assert meta.embedding_model == "hash"
    assert artifact.embeddings.shape == (len(artifact.nodes), meta.embedding_dims)


def test_artifact_with_other_format_version_is_refused(
    artifact: GraphArtifact, tmp_path: Path
) -> None:
    path = tmp_path / "graph.wgz"
    artifact.save(path)
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    meta = json.loads(files["meta.json"])
    meta["format_version"] = 99
    files["meta.json"] = json.dumps(meta).encode()
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    with pytest.raises(ValueError, match="rebuild"):
        GraphArtifact.load(path)


def test_build_is_deterministic(source: DbtArtifacts, artifact: GraphArtifact) -> None:
    again = build(
        source,
        HashEmbedder(),
        caveats=FIXTURE / "caveats.yml",
        query_examples=FIXTURE / "query_examples.yml",
    )
    assert again.nodes == artifact.nodes and again.edges == artifact.edges
    assert (again.embeddings == artifact.embeddings).all()


def test_node_text_includes_columns_and_labels(artifact: GraphArtifact) -> None:
    text = node_text(artifact.nodes["metric.ad_spend_nzd"])
    assert "Ad spend" in text and "metric" in text
    assert tokenize("fct_ad_spend.daily") == ["fct", "ad", "spend", "daily"]


# -- query API (2.6) ----------------------------------------------------------------------------


def test_search_finds_obvious_node(q: GraphQuery) -> None:
    hits = q.search_nodes("ad spend metric", k=5)
    assert "metric.ad_spend_nzd" in [h.id for h in hits]
    assert hits == sorted(hits, key=lambda h: -h.score)


def test_search_filters_by_type(q: GraphQuery) -> None:
    hits = q.search_nodes("salary", types=["column"], k=3)
    assert hits and all(h.type is NodeType.COLUMN for h in hits)
    assert hits[0].id.endswith("annual_salary")


def test_search_reports_trap_flags(q: GraphQuery) -> None:
    hits = q.search_nodes("orders v1 deprecated", types=[NodeType.MODEL], k=1)
    assert hits[0].id == "model.orders_v1"
    assert hits[0].deprecated and hits[0].trap_kind == "deprecated_model"


def test_get_node_includes_caveats_columns_and_neighbours(q: GraphQuery) -> None:
    rec = q.get_node("model.fct_ad_spend_daily")
    assert [c["id"] for c in rec.caveats] == ["caveat.cav_spend_delay"]
    assert "spend_nzd" in [c.name for c in rec.columns]
    assert rec.upstream == ["model.int_marketing__spend_daily"]
    assert set(rec.downstream) == {
        "exposure.marketing_performance_dashboard",
        "metric.ad_spend_nzd",
    }


def test_get_node_unknown_id(q: GraphQuery) -> None:
    with pytest.raises(KeyError, match="search_nodes"):
        q.get_node("model.nope")


def test_lineage_upstream_respects_depth(q: GraphQuery) -> None:
    one = q.get_lineage("model.fct_ad_spend_daily", "upstream", depth=1)
    assert {n.id for n in one.nodes} == {
        "model.fct_ad_spend_daily",
        "model.int_marketing__spend_daily",
    }
    full = q.get_lineage("model.fct_ad_spend_daily", "upstream", depth=10)
    assert {"source.marketing.ad_spend", "source.marketing.campaigns"} <= {n.id for n in full.nodes}
    assert all(e.type is EdgeType.DEPENDS_ON for e in full.edges)


def test_lineage_downstream_reaches_exposures_and_metrics(q: GraphQuery) -> None:
    sub = q.get_lineage("source.marketing.ad_spend", "downstream", depth=10)
    ids = {n.id for n in sub.nodes}
    assert {"exposure.marketing_performance_dashboard", "metric.ad_spend_nzd"} <= ids


def test_column_level_lineage(q: GraphQuery) -> None:
    sub = q.get_lineage("column.fct_ad_spend_daily.spend_nzd", "upstream", depth=10, level="column")
    ids = {n.id for n in sub.nodes}
    assert "column.marketing.ad_spend.spend" in ids
    assert all(e.type is EdgeType.COLUMN_DERIVES_FROM for e in sub.edges)
    # A model at column level starts from all of its columns.
    model = q.get_lineage("model.fct_ad_spend_daily", "upstream", depth=1, level="column")
    assert "column.int_marketing__spend_daily.spend_nzd" in {n.id for n in model.nodes}


def test_blast_radius_of_source(q: GraphQuery) -> None:
    br = q.get_blast_radius("source.marketing.ad_spend")
    assert [e.id for e in br.exposures] == ["exposure.marketing_performance_dashboard"]
    assert br.max_criticality == "medium"
    assert [m.id for m in br.metrics] == ["metric.ad_spend_nzd"]
    models = [m.id for m in br.models]
    assert models[0] == "model.stg_marketing__ad_spend"  # same criticality, closest first
    assert "model.fct_ad_spend_daily" in models


def test_blast_radius_of_column(q: GraphQuery) -> None:
    br = q.get_blast_radius("column.marketing.ad_spend.spend")
    assert [e.id for e in br.exposures] == ["exposure.marketing_performance_dashboard"]


def test_blast_radius_with_no_exposure(q: GraphQuery) -> None:
    br = q.get_blast_radius("model.stg_hr__employees")
    assert br.exposures == [] and br.max_criticality is None


def test_get_metric(q: GraphQuery) -> None:
    rec = q.get_metric("ad_spend_nzd")
    assert rec.sql.startswith("select sum(spend_nzd)")
    assert rec.model == "model.fct_ad_spend_daily"
    assert rec.canonical_alternative is None and rec.inputs == []
    assert [c["id"] for c in rec.caveats] == ["caveat.cav_spend_delay"]
    assert q.get_metric("metric.ad_spend_nzd") == rec
    with pytest.raises(KeyError):
        q.get_metric("model.fct_orders")


# -- rendering, config, CLI ---------------------------------------------------------------------


def test_mermaid_lineage(q: GraphQuery) -> None:
    text = mermaid(q.get_lineage("model.orders_v1", "upstream", depth=5))
    assert text.startswith("flowchart LR")
    assert "model_stg_shop__orders --> model_orders_v1" in text
    assert "orders_v1 ⚠" in text and "class model_orders_v1 flagged" in text


def test_stats_markdown(artifact: GraphArtifact) -> None:
    md = stats_markdown(artifact.meta)
    assert "| model | 9 |" in md and "Coverage:" in md


def test_config_paths_are_relative_to_file(tmp_path: Path) -> None:
    cfg = tmp_path / "warehouse-graph.toml"
    cfg.write_text('[graph]\noutput = "out/g.wgz"\nembedding_model = "hash"\n', encoding="utf-8")
    loaded = GraphConfig.load(cfg)
    assert loaded.output == tmp_path / "out" / "g.wgz"
    assert loaded.embedding_model == "hash"
    assert GraphConfig.load(tmp_path / "missing.toml") == GraphConfig()


def test_cli_build_and_lineage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "warehouse-graph.toml"
    fixture = FIXTURE.as_posix()
    cfg.write_text(
        f'[graph]\nartifacts_dir = "{fixture}"\ncaveats = "{fixture}/caveats.yml"\n'
        f'query_examples = "{fixture}/query_examples.yml"\noutput = "g.wgz"\n',
        encoding="utf-8",
    )
    stats = tmp_path / "stats.md"
    assert (
        main(["build", "--config", str(cfg), "--embedding-model", "hash", "--stats", str(stats)])
        == 0
    )
    assert (tmp_path / "g.wgz").exists() and stats.exists()
    capsys.readouterr()
    assert main(["lineage", "model.orders_v1", "--graph", str(tmp_path / "g.wgz")]) == 0
    assert capsys.readouterr().out.startswith("flowchart LR")


def test_search_modes(q: GraphQuery) -> None:
    hybrid = q.search_nodes("ad spend metric", k=5)
    vector = q.search_nodes("ad spend metric", k=5, mode="vector")
    bm25 = q.search_nodes("ad spend metric", k=5, mode="bm25")
    assert all(h.vector_rank == i for i, h in enumerate(vector, 1))
    assert all(h.bm25_rank == i for i, h in enumerate(bm25, 1))
    assert [h.score for h in vector] == sorted((h.score for h in vector), reverse=True)
    assert hybrid and vector and bm25


def test_freshness_propagates_to_downstream_models(artifact: GraphArtifact) -> None:
    from warehouse_graph.graph.enrich import propagate_freshness

    nodes = [
        n.model_copy(update={"freshness_status": "error"})
        if n.id == "source.marketing.ad_spend"
        else n
        for n in artifact.nodes.values()
    ]
    out = {n.id: n for n in propagate_freshness(nodes, artifact.edges)}
    spend = out["model.fct_ad_spend_daily"]
    assert isinstance(spend, ModelNode)
    assert spend.upstream_freshness == "error"
    assert spend.upstream_freshness_note and "marketing" not in spend.upstream_freshness_note
    orders = out["model.orders_v1"]
    assert isinstance(orders, ModelNode) and orders.upstream_freshness in ("pass", None)
