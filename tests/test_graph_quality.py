"""Graph quality tests on the full demo warehouse (task 2.7).

Needs `demo/artifacts/` (from `make warehouse`) and skips without it, like test_demo_assets.
The graph is built with `HashEmbedder`: these tests check structure, not search quality.
The metric-SQL test is `integration`: it runs every metric's canonical SQL on local Postgres.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from warehouse_graph.demo.load import conninfo_from_env
from warehouse_graph.graph import DbtArtifacts, GraphArtifact, GraphQuery, build
from warehouse_graph.graph.embed import HashEmbedder
from warehouse_graph.graph.schema import (
    CaveatNode,
    ColumnNode,
    EdgeType,
    MetricNode,
    ModelNode,
    SourceNode,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
ARTIFACTS = DEMO / "artifacts"
PII_COLUMNS = {"first_name", "last_name", "email", "work_email", "phone", "annual_salary"}


@pytest.fixture(scope="module")
def artifact() -> GraphArtifact:
    if not (ARTIFACTS / "manifest.json").exists():
        pytest.skip("no demo/artifacts; run `make warehouse`")
    return build(
        DbtArtifacts(ARTIFACTS),
        HashEmbedder(),
        caveats=DEMO / "caveats.yml",
        query_examples=DEMO / "query_examples.yml",
    )


@pytest.fixture(scope="module")
def q(artifact: GraphArtifact) -> GraphQuery:
    return GraphQuery(artifact, embedder=HashEmbedder())


def test_counts_match_the_project(artifact: GraphArtifact) -> None:
    counts = artifact.meta.stats.nodes_by_type
    assert (counts["model"], counts["source"], counts["exposure"]) == (40, 11, 6)
    assert (counts["metric"], counts["caveat"], counts["query_example"]) == (13, 15, 30)


def test_no_orphan_columns(artifact: GraphArtifact) -> None:
    owners: dict[str, list[str]] = {}
    for e in artifact.edges:
        if e.type is EdgeType.HAS_COLUMN:
            owners.setdefault(e.target, []).append(e.source)
    for n in artifact.nodes.values():
        if isinstance(n, ColumnNode):
            assert owners.get(n.id) == [n.parent], f"{n.id} is orphaned or has several parents"


def test_every_mart_is_reachable_from_a_source(q: GraphQuery, artifact: GraphArtifact) -> None:
    marts = [n for n in artifact.nodes.values() if isinstance(n, ModelNode) and n.layer == "marts"]
    assert len(marts) == 19  # + metricflow_time_spine (layer utility), generated, no source
    for m in marts:
        upstream = q.get_lineage(m.id, "upstream", depth=50).nodes
        assert any(isinstance(artifact.nodes[u.id], SourceNode) for u in upstream), m.id


def test_column_lineage_coverage(artifact: GraphArtifact) -> None:
    stats = artifact.meta.stats
    assert not stats.lineage_failed_models
    assert stats.column_lineage_coverage >= 0.95  # 97.8% at Phase 2; a drop means a regression


# Planted traps (docs/planted-traps.md): node id -> expected trap kind.
TRAP_KINDS = {
    "model.orders_v1": "deprecated_model",  # T1
    "column.dim_customers.legacy_segment": "deprecated_column",  # T2
    "metric.revenue": "competing_metric",  # T4
    "metric.churn_rate_marketing": "competing_metric",  # T5
    "column.rpt_customer_churn.is_churned_marketing": "competing_metric",  # T5
    "model.rpt_regional_sales_legacy": "fanout_join",  # T6
    "model.fct_payments": "fanout_join",  # T7
    "model.fct_sales": "misleading_name",  # T8
    "model.rpt_active_customers": "misleading_name",  # T9
}


@pytest.mark.parametrize(("node_id", "kind"), TRAP_KINDS.items())
def test_trap_is_flagged(artifact: GraphArtifact, node_id: str, kind: str) -> None:
    trap = artifact.nodes[node_id].trap
    assert trap is not None and trap.kind == kind


def test_deprecations_point_at_replacements(artifact: GraphArtifact) -> None:
    deprecated = {n.id: n.replaced_by for n in artifact.nodes.values() if n.deprecated}
    assert deprecated == {
        "model.orders_v1": "model.fct_orders",
        "column.dim_customers.legacy_segment": "column.dim_customers.customer_segment",
        "column.stg_shop__customers.legacy_segment": "column.dim_customers.customer_segment",
    }


def test_stale_able_source(artifact: GraphArtifact) -> None:  # T3
    s = artifact.nodes["source.marketing.ad_spend"]
    assert isinstance(s, SourceNode) and s.stale_on_demand
    assert (s.freshness_warn_hours, s.freshness_error_hours) == (24.0, 48.0)


def test_competing_metrics_point_at_canonical(q: GraphQuery) -> None:  # T4, T5
    for trap, canonical in [("revenue", "net_revenue"), ("churn_rate_marketing", "churn_rate")]:
        assert q.get_metric(trap).canonical_alternative == f"metric.{canonical}"
        rec = q.get_metric(canonical)
        assert rec.metric["canonical"] and rec.canonical_alternative is None


@pytest.mark.parametrize(
    ("caveat", "attached_to"),
    [
        ("cav_refunds_before_2024_03", "source.shop.refunds"),  # T10
        ("cav_fixed_fx_rate", "column.fct_orders.order_total_nzd"),  # T11
        ("cav_order_region_vs_customer_region", "model.fct_orders"),  # T12
        ("cav_as_of_date", "model.rpt_customer_churn"),  # T13
        ("cav_ad_spend_vendor_delay", "source.marketing.ad_spend"),  # T3
        ("cav_two_churn_definitions", "metric.churn_rate_marketing"),  # T5
    ],
)
def test_caveat_is_attached(q: GraphQuery, caveat: str, attached_to: str) -> None:
    assert f"caveat.{caveat}" in [c["id"] for c in q.get_node(attached_to).caveats]


def test_every_caveat_is_attached_somewhere(artifact: GraphArtifact) -> None:
    attached = {e.target for e in artifact.edges if e.type is EdgeType.HAS_CAVEAT}
    caveats = {n.id for n in artifact.nodes.values() if isinstance(n, CaveatNode)}
    assert caveats == attached


def test_every_query_example_reads_a_model(artifact: GraphArtifact) -> None:
    linked = {e.target for e in artifact.edges if e.type is EdgeType.ANSWERED_BY}
    examples = {n.id for n in artifact.nodes.values() if n.id.startswith("query_example.")}
    assert examples == linked


def test_pii_is_tagged_from_raw_to_marts(artifact: GraphArtifact) -> None:
    for n in artifact.nodes.values():
        if isinstance(n, ColumnNode) and n.name in PII_COLUMNS:
            assert n.pii and n.sensitivity, f"{n.id} is not tagged as PII"
    salary = [n for n in artifact.nodes.values() if isinstance(n, ColumnNode) and n.pii == "salary"]
    assert {n.id for n in salary} == {
        "column.hr.employees.annual_salary",
        "column.stg_hr__employees.annual_salary",
        "column.int_employees__store_staff.annual_salary",
        "column.dim_employees.annual_salary",
    }
    assert all(n.sensitivity == "restricted" for n in salary)


@pytest.mark.parametrize(
    ("model", "exposures"),
    [
        (
            "stg_shop__orders",
            [  # high first, then by distance
                "store_operations_dashboard",
                "executive_revenue_dashboard",
                "customer_health_dashboard",
                "marketing_performance_dashboard",
                "regional_sales_legacy_dashboard",
            ],
        ),
        ("stg_marketing__ad_spend", ["marketing_performance_dashboard"]),
        (
            "stg_hr__employees",
            [
                "payroll_cost_report",
                "store_operations_dashboard",
                "regional_sales_legacy_dashboard",
            ],
        ),
    ],
)
def test_blast_radius_of_staging_model(q: GraphQuery, model: str, exposures: list[str]) -> None:
    br = q.get_blast_radius(f"model.{model}")
    assert [e.name for e in br.exposures] == exposures
    assert br.max_criticality == br.exposures[0].criticality


def test_blast_radius_is_ranked_by_criticality(q: GraphQuery) -> None:
    from warehouse_graph.graph.schema import CRITICALITY_RANK

    br = q.get_blast_radius("source.shop.orders")
    ranks = [CRITICALITY_RANK[e.criticality] for e in br.exposures]
    assert ranks == sorted(ranks, reverse=True)
    model_ranks = [CRITICALITY_RANK[m.criticality] for m in br.models]
    assert model_ranks == sorted(model_ranks, reverse=True)


@pytest.mark.integration
def test_every_metric_sql_runs(artifact: GraphArtifact) -> None:
    metrics = [n for n in artifact.nodes.values() if isinstance(n, MetricNode)]
    with psycopg.connect(conninfo_from_env()) as conn:
        values = {m.name: conn.execute(m.sql).fetchone() for m in metrics}
    assert all(v is not None and v[0] is not None for v in values.values())
    churn = values["churn_rate"]
    assert churn is not None and round(float(churn[0]), 3) == 0.733  # planted-traps.md T5
    marketing = values["churn_rate_marketing"]
    assert marketing is not None and round(float(marketing[0]), 3) == 0.630
