"""Unit tests for policy-as-code on the mini fixture: loading, resolving, the Postgres compiler,
role-filtered metadata and the description lint. No database (see test_policies_db.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from warehouse_graph.adapters import Rows
from warehouse_graph.governance import load_policies, resolve, resolve_principal
from warehouse_graph.governance.lint import lint_descriptions
from warehouse_graph.governance.metadata import filter_graph
from warehouse_graph.governance.policies import PolicySpec
from warehouse_graph.governance.postgres import compile_postgres, redact
from warehouse_graph.graph import DbtArtifacts, GraphArtifact, GraphQuery, build
from warehouse_graph.graph.embed import HashEmbedder
from warehouse_graph.graph.schema import ColumnNode, ModelNode

FIXTURE = Path(__file__).parent / "fixtures" / "dbt_mini"
POLICIES = FIXTURE / "policies.yml"


@pytest.fixture(scope="module")
def graph() -> GraphArtifact:
    return build(
        DbtArtifacts(FIXTURE),
        HashEmbedder(),
        caveats=FIXTURE / "caveats.yml",
        query_examples=FIXTURE / "query_examples.yml",
    )


@pytest.fixture(scope="module")
def spec(graph: GraphArtifact) -> PolicySpec:
    return resolve(load_policies(POLICIES), graph)


@pytest.fixture(scope="module")
def sql(spec: PolicySpec) -> list[str]:
    return compile_postgres(spec)


def _write(tmp_path: Path, **changes: object) -> Path:
    data = yaml.safe_load(POLICIES.read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / "policies.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# -- loading and identities (3.2) ---------------------------------------------------------------


def test_the_real_policy_file_loads() -> None:
    policies = load_policies(Path(__file__).parents[1] / "policies.yml")
    assert set(policies.principals) == {
        "admin",
        "finance",
        "marketing",
        "analyst_au",
        "analyst_nz",
        "sentinel",
    }


def test_identity_maps_to_principal() -> None:
    policies = load_policies(POLICIES)
    assert resolve_principal(policies, "au@example").name == "analyst_au"
    with pytest.raises(PermissionError):
        resolve_principal(policies, "stranger@example")


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"identities": {"x@example": "nobody"}}, "unknown principal"),
        ({"audit_readers": ["nobody"]}, "audit reader"),
        ({"version": 2}, "version"),
    ],
)
def test_invalid_policy_files_are_refused(
    tmp_path: Path, change: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        load_policies(_write(tmp_path, **change))


def test_unsafe_role_name_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(POLICIES.read_text(encoding="utf-8"))
    data["principals"]["Robert'); drop"] = data["principals"]["admin"]
    with pytest.raises(ValueError, match="safe database role name"):
        load_policies(_write(tmp_path, principals=data["principals"]))


def test_row_filter_outside_view_schemas_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(POLICIES.read_text(encoding="utf-8"))
    data["principals"]["analyst_au"]["schemas"] = ["marts", "staging"]
    with pytest.raises(ValueError, match="row filter"):
        load_policies(_write(tmp_path, principals=data["principals"]))


def test_selector_that_matches_nothing_is_refused(tmp_path: Path, graph: GraphArtifact) -> None:
    data = yaml.safe_load(POLICIES.read_text(encoding="utf-8"))
    data["principals"]["marketing"]["hidden"] = ["pii:passport"]
    with pytest.raises(ValueError, match="matches no column"):
        resolve(load_policies(_write(tmp_path, principals=data["principals"])), graph)


# -- resolution ---------------------------------------------------------------------------------


def test_schemas_limit_relations(spec: PolicySpec) -> None:
    assert len(spec.access["admin"].relations) == len(spec.relations)
    marketing = {r.schema for r in spec.access["marketing"].relations.values()}
    assert marketing == {"staging", "marts"}


def test_row_filter_hides_tables_without_the_column(spec: PolicySpec) -> None:
    au = spec.access["analyst_au"].relations
    assert "model.fct_orders" in au and "model.fct_ad_spend_daily" in au
    assert "model.orders_v1" not in au  # no region_code: can't be filtered, so denied


def test_hidden_and_masked_columns(spec: PolicySpec) -> None:
    marketing = spec.access["marketing"]
    assert "column.stg_hr__employees.annual_salary" in marketing.hidden_columns
    assert "column.stg_hr__employees.work_email" in marketing.hidden_columns
    # Masking needs a policy view; staging has none, so masked falls back to hidden.
    assert "column.stg_hr__employees.first_name" in marketing.hidden_columns
    au = spec.access["analyst_au"]
    assert au.masked_columns == {"column.fct_orders.discount_code": "redact"}


def test_sentinel_sees_every_layer_without_pii(spec: PolicySpec, graph: GraphArtifact) -> None:
    sentinel = spec.access["sentinel"]
    pii = {n.id for n in graph.nodes.values() if isinstance(n, ColumnNode) and n.pii}
    assert pii and pii <= sentinel.hidden_columns
    assert "source.hr.employees" in sentinel.relations


# -- Postgres compiler (3.3) --------------------------------------------------------------------


def test_one_login_role_per_principal(sql: list[str]) -> None:
    for p in ["admin", "analyst_au", "marketing", "sentinel"]:
        role = [s for s in sql if s.startswith(f'alter role "{p}" with login')]
        assert len(role) == 1
        assert "noinherit" in role[0] and "nobypassrls" in role[0] and "nosuperuser" in role[0]
    assert 'alter role "analyst_au" set default_transaction_read_only = on' in sql
    assert "alter role \"analyst_au\" set statement_timeout = '30s'" in sql


def test_marts_tables_move_behind_views(sql: list[str]) -> None:
    view = next(s for s in sql if s.startswith('create view "marts"."fct_orders"'))
    assert "with (security_barrier)" in view
    assert view.endswith('from "marts_base"."fct_orders"')
    assert 'policy.mask_redact("discount_code")' in view
    assert "current_user in ('analyst_au')" in view
    assert 'alter view "marts"."fct_orders" owner to "policy_owner"' in sql
    # Staging is not served through views.
    assert not any(s.startswith('create view "staging"') for s in sql)


def test_rls_predicate_keyed_on_current_user(sql: list[str]) -> None:
    policy = next(
        s for s in sql if s.startswith('create policy wg_rows on "marts_base"."fct_orders"')
    )
    assert "case current_user when 'analyst_au' then \"region_code\" = 'AU'" in policy
    assert 'to "policy_owner"' in policy


def test_hidden_columns_are_never_granted(sql: list[str]) -> None:
    grants = [s for s in sql if s.startswith("grant select (") and s.endswith('to "marketing"')]
    staff = next(g for g in grants if '"staging"."stg_hr__employees"' in g)
    assert '"annual_salary"' not in staff and '"work_email"' not in staff
    assert '"employee_id"' in staff


def test_revoke_comes_before_grant(sql: list[str]) -> None:
    revoke = sql.index(
        next(
            s for s in sql if s.startswith("revoke all on all tables") and s.endswith('"marketing"')
        )
    )
    first_grant = sql.index(
        next(s for s in sql if s.startswith("grant") and s.endswith('"marketing"'))
    )
    assert revoke < first_grant


def test_audit_table_is_append_only(sql: list[str]) -> None:
    script = "\n".join(sql)
    assert "create table if not exists audit.events" in script
    assert "before update or delete on audit.events" in script
    assert "before truncate on audit.events" in script
    assert 'grant insert, select (event_id) on audit.events to "audit_writer"' in script
    assert 'grant select on audit.events to "admin"' in script
    assert 'grant select on audit.events to "marketing"' not in script


def test_compile_is_deterministic(spec: PolicySpec, sql: list[str]) -> None:
    assert compile_postgres(spec) == sql


def test_passwords_are_redacted_when_printed(sql: list[str]) -> None:
    role = next(s for s in sql if " password " in s)
    assert redact(role).endswith("password '***'")


# -- role-filtered metadata (3.5) ---------------------------------------------------------------


def _q(graph: GraphArtifact, spec: PolicySpec, principal: str) -> GraphQuery:
    visible = filter_graph(graph, spec.access[principal], HashEmbedder())
    return GraphQuery(visible, embedder=HashEmbedder())


def test_hidden_column_is_indistinguishable_from_missing(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    q = _q(graph, spec, "marketing")
    errors = []
    for node_id in ["column.stg_hr__employees.annual_salary", "column.stg_hr__employees.nope"]:
        with pytest.raises(KeyError) as e:
            q.get_node(node_id)
        errors.append(str(e.value).replace(node_id, "<id>"))
    assert errors[0] == errors[1]


def test_hidden_columns_leave_every_view_of_the_graph(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    q = _q(graph, spec, "marketing")
    rec = q.get_node("model.stg_hr__employees")
    assert "annual_salary" not in {c.name for c in rec.columns}
    hits = q.search_nodes("annual salary", k=50)
    assert not any("annual_salary" in h.id for h in hits)
    lineage = q.get_lineage("model.stg_hr__employees", "both", depth=5, level="column")
    assert not any("annual_salary" in n.id for n in lineage.nodes)


def test_masked_columns_are_flagged(graph: GraphArtifact, spec: PolicySpec) -> None:
    q = _q(graph, spec, "analyst_au")
    assert q.get_node("column.fct_orders.discount_code").node["masked"] is True
    assert q.get_node("column.fct_orders.order_id").node["masked"] is False


def test_unqueryable_relations_and_dependents_disappear(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    au = filter_graph(graph, spec.access["analyst_au"], HashEmbedder())
    assert "model.orders_v1" not in au.nodes
    assert "caveat.cav_orders_v1" not in au.nodes  # only attached to orders_v1
    assert "source.shop.orders" not in au.nodes
    assert all(e.source in au.nodes and e.target in au.nodes for e in au.edges)
    assert au.embeddings.shape[0] == len(au.nodes)


def test_compiled_sql_only_for_principals_who_see_everything(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    admin = filter_graph(graph, spec.access["admin"], HashEmbedder())
    marketing = filter_graph(graph, spec.access["marketing"], HashEmbedder())
    for view, expected in [(admin, True), (marketing, False)]:
        model = view.nodes["model.stg_hr__employees"]
        assert isinstance(model, ModelNode)
        assert bool(model.compiled_sql) is expected


def test_relations_with_hidden_columns_are_re_embedded(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    marketing = filter_graph(graph, spec.access["marketing"], HashEmbedder())
    staff = "model.stg_hr__employees"
    before = graph.embeddings[graph.row[staff]]
    after = marketing.embeddings[marketing.row[staff]]
    assert not (before == after).all()
    untouched = "model.fct_ad_spend_daily"
    assert (
        graph.embeddings[graph.row[untouched]] == marketing.embeddings[marketing.row[untouched]]
    ).all()


def test_query_example_reading_hidden_table_is_removed(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    sentinel = filter_graph(graph, spec.access["sentinel"], HashEmbedder())
    assert "query_example.qe01" in sentinel.nodes
    au = filter_graph(graph, spec.access["analyst_au"], HashEmbedder())
    assert {"query_example.qe01", "query_example.qe02"} <= set(au.nodes)


# -- description lint (3.6) ---------------------------------------------------------------------


def test_lint_is_clean_on_fixture(graph: GraphArtifact, spec: PolicySpec) -> None:
    assert lint_descriptions(spec, graph) == []


def test_lint_flags_description_that_names_a_hidden_column(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    node = graph.nodes["model.stg_hr__employees"]
    leaky = node.model_copy(update={"description": "Staff list including annual_salary."})
    nodes = {**graph.nodes, node.id: leaky}
    leaky_graph = GraphArtifact(graph.meta, nodes, graph.edges, graph.embeddings)
    findings = lint_descriptions(spec, leaky_graph)
    assert {(f.principal, f.node_id) for f in findings} == {
        ("marketing", "model.stg_hr__employees"),
        ("sentinel", "model.stg_hr__employees"),
    }
    assert all(f.hidden_column.endswith("annual_salary") for f in findings)


def test_lint_flags_pii_kind_words(graph: GraphArtifact, spec: PolicySpec) -> None:
    node = graph.nodes["model.stg_hr__employees"]
    leaky = node.model_copy(update={"description": "Includes each person's salary."})
    leaky_graph = GraphArtifact(
        graph.meta, {**graph.nodes, node.id: leaky}, graph.edges, graph.embeddings
    )
    assert any(f.term == "salary" for f in lint_descriptions(spec, leaky_graph))


# -- result stats (what an LLM may see) ---------------------------------------------------------


def test_rows_stats_never_include_values_beyond_min_max() -> None:
    rows = Rows(
        columns=("region", "amount"),
        rows=[("AU", 10.0), ("NZ", None), ("NZ", 30.0)],
        truncated=False,
        elapsed_s=0.01,
    )
    stats = rows.stats()
    assert stats.row_count == 3
    region, amount = stats.columns
    assert (region.distinct, region.min, region.max) == (2, "AU", "NZ")
    assert (amount.null_rate, amount.min, amount.max) == (0.3333, 10.0, 30.0)


def test_filter_without_embedder_zeroes_affected_rows(
    graph: GraphArtifact, spec: PolicySpec
) -> None:
    marketing = filter_graph(graph, spec.access["marketing"], None)
    assert not marketing.embeddings[marketing.row["model.stg_hr__employees"]].any()


def test_filter_refuses_a_different_embedding_model(graph: GraphArtifact, spec: PolicySpec) -> None:
    class Other(HashEmbedder):
        name = "some-other-model"

    with pytest.raises(ValueError, match="embedded with"):
        filter_graph(graph, spec.access["marketing"], Other())
