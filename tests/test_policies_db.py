"""Policy DB tests (task 3.4) and the Phase 3 gate, against the real warehouse.

Plain SQL, logged in as each principal's own role, no LLM involved. Needs `make policies`
(Postgres up, warehouse built, graph built, policies applied); integration tests skip otherwise.
The MCP half of the gate needs only `demo/artifacts/`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp import Client
from mcp.types import TextContent

from warehouse_graph.adapters import PostgresAdapter
from warehouse_graph.adapters.postgres import principal_conninfo
from warehouse_graph.demo.load import conninfo_from_env
from warehouse_graph.governance import Principal, load_policies, resolve
from warehouse_graph.governance.audit import AuditWriter
from warehouse_graph.governance.lint import lint_descriptions
from warehouse_graph.graph import DbtArtifacts, GraphArtifact, build
from warehouse_graph.graph.embed import HashEmbedder
from warehouse_graph.mcp_server import create_server

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
POLICIES = ROOT / "policies.yml"
PRINCIPALS = ["admin", "finance", "marketing", "analyst_au", "analyst_nz", "sentinel"]
DENIED = psycopg.errors.InsufficientPrivilege
MASKED_EMAIL = re.compile(r"^.\*\*\*@\*\*\*(\.[A-Za-z]+)?$")


def _connect(role: str) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(principal_conninfo(Principal(role, "")), autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("principal roles missing; run `make policies`")


@pytest.fixture(scope="module")
def roles() -> Iterator[dict[str, psycopg.Connection[Any]]]:
    conns = {r: _connect(r) for r in PRINCIPALS}
    yield conns
    for c in conns.values():
        c.close()


@pytest.fixture(scope="module")
def owner() -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(conninfo_from_env(), autocommit=True) as conn:
        yield conn


def _one(conn: psycopg.Connection[Any], sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _denied(conn: psycopg.Connection[Any], sql: str) -> None:
    with pytest.raises(DENIED):
        conn.execute(sql)


# -- rows (RLS) ---------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(("role", "region"), [("analyst_au", "AU"), ("analyst_nz", "NZ")])
def test_analysts_see_only_their_region(
    roles: dict[str, psycopg.Connection[Any]],
    owner: psycopg.Connection[Any],
    role: str,
    region: str,
) -> None:
    conn = roles[role]
    expected = _one(owner, f"select count(*) from marts.fct_orders where region_code = '{region}'")
    assert _one(conn, "select count(*) from marts.fct_orders") == expected > 0
    tables = [
        r[0]
        for r in owner.execute(
            "select table_name from information_schema.columns"
            " where table_schema = 'marts' and column_name = 'region_code'"
        )
    ]
    assert len(tables) == 17
    for t in tables:
        other = _one(conn, f"select count(*) from marts.{t} where region_code <> '{region}'")
        assert other == 0, f"{role} sees other-region rows in {t}"


@pytest.mark.integration
@pytest.mark.parametrize("role", ["admin", "finance", "marketing", "sentinel"])
def test_unfiltered_roles_see_every_region(
    roles: dict[str, psycopg.Connection[Any]], role: str
) -> None:
    assert _one(roles[role], "select count(distinct region_code) from marts.fct_orders") == 2


@pytest.mark.integration
def test_tables_that_cant_be_filtered_are_denied_to_analysts(
    roles: dict[str, psycopg.Connection[Any]],
) -> None:
    _denied(roles["analyst_au"], "select count(*) from marts.orders_v1")  # no region_code
    assert _one(roles["analyst_au"], "select count(*) from marts.dim_products") > 0  # unfiltered


# -- columns: hidden and masked -----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("role", "sql"),
    [
        ("marketing", "select email from marts.dim_customers"),
        ("marketing", "select phone from marts.dim_customers"),
        ("marketing", "select annual_salary from marts.dim_employees"),
        ("marketing", "select work_email from marts.dim_employees"),
        ("marketing", "select * from marts.dim_customers"),
        ("analyst_au", "select annual_salary from marts.dim_employees"),
        ("analyst_nz", "select annual_salary from marts.dim_employees"),
        ("sentinel", "select email from marts.dim_customers"),
        ("sentinel", "select email from raw.customers"),
        ("sentinel", "select email from staging.stg_shop__customers"),
        ("sentinel", "select annual_salary from raw.employees"),
        ("sentinel", "select first_name from marts.dim_customers"),
    ],
)
def test_hidden_columns_are_denied(
    roles: dict[str, psycopg.Connection[Any]], role: str, sql: str
) -> None:
    _denied(roles[role], sql)


@pytest.mark.integration
@pytest.mark.parametrize("role", ["analyst_au", "analyst_nz", "finance"])
def test_emails_are_masked(roles: dict[str, psycopg.Connection[Any]], role: str) -> None:
    emails = [r[0] for r in roles[role].execute("select email from marts.dim_customers")]
    assert emails and all(MASKED_EMAIL.match(e) for e in emails if e is not None)
    phones = [r[0] for r in roles[role].execute("select phone from marts.dim_customers")]
    assert all(p.startswith("***-***-") for p in phones if p is not None)


@pytest.mark.integration
def test_masking_is_per_role(roles: dict[str, psycopg.Connection[Any]]) -> None:
    au_name = _one(roles["analyst_au"], "select first_name from marts.dim_customers limit 1")
    assert au_name.endswith("***")
    marketing_name = _one(roles["marketing"], "select first_name from marts.dim_customers limit 1")
    assert marketing_name.endswith("***")
    finance_name = _one(roles["finance"], "select first_name from marts.dim_customers limit 1")
    assert "***" not in finance_name
    assert "***" not in _one(roles["admin"], "select email from marts.dim_customers limit 1")
    assert _one(roles["finance"], "select max(annual_salary) from marts.dim_employees") > 0


@pytest.mark.integration
def test_filtering_on_a_masked_column_cant_recover_values(
    roles: dict[str, psycopg.Connection[Any]], owner: psycopg.Connection[Any]
) -> None:
    real = _one(
        owner, "select email from marts_base.dim_customers where region_code = 'AU' limit 1"
    )
    found = _one(
        roles["analyst_au"], f"select count(*) from marts.dim_customers where email = '{real}'"
    )
    assert found == 0


# -- reach: schemas, escalation, writes ---------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from marts_base.fct_orders",
        "select count(*) from staging.stg_shop__orders",
        "select count(*) from intermediate.int_orders__enriched",
        "select count(*) from raw.orders",
        "select count(*) from audit.events",
    ],
)
def test_analysts_reach_only_marts(roles: dict[str, psycopg.Connection[Any]], sql: str) -> None:
    _denied(roles["analyst_au"], sql)


@pytest.mark.integration
def test_no_role_escalation(roles: dict[str, psycopg.Connection[Any]]) -> None:
    for role in ["analyst_au", "marketing", "sentinel"]:
        _denied(roles[role], "set role admin")
        _denied(roles[role], "set role warehouse")
        assert (
            _one(
                roles[role],
                "select rolsuper or rolbypassrls from pg_roles where rolname = current_user",
            )
            is False
        )


@pytest.mark.integration
def test_writes_fail_even_after_leaving_read_only(
    roles: dict[str, psycopg.Connection[Any]],
) -> None:
    conn = roles["analyst_au"]
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("create table marts.x (i int)")
    conn.execute("set default_transaction_read_only = off")
    try:
        _denied(conn, "create table marts.x (i int)")
        _denied(conn, "insert into marts.dim_products (product_id) values (-1)")
        _denied(conn, "delete from marts.dim_products")
    finally:
        conn.execute("reset default_transaction_read_only")


@pytest.mark.integration
def test_role_settings(roles: dict[str, psycopg.Connection[Any]]) -> None:
    for role in PRINCIPALS:
        assert _one(roles[role], "show statement_timeout") == "30s"
        assert _one(roles[role], "show default_transaction_read_only") == "on"


@pytest.mark.integration
def test_sentinel_gets_diagnostics_without_pii(roles: dict[str, psycopg.Connection[Any]]) -> None:
    conn = roles["sentinel"]
    assert _one(conn, "select count(*) from raw.orders") > 0
    assert _one(conn, "select count(*) from staging.stg_shop__customers where city is not null") > 0
    assert _one(conn, "select max(_loaded_at) from raw.ad_spend") is not None


# -- audit (3.9) ---------------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_log_is_append_only(
    roles: dict[str, psycopg.Connection[Any]], owner: psycopg.Connection[Any]
) -> None:
    writer = AuditWriter()
    try:
        event_id = writer.write("analyst_au", "test.event", {"question": "q", "nodes": ["a"]})
    finally:
        writer.close()
    row = owner.execute(
        "select principal, event_type, payload from audit.events where event_id = %s", (event_id,)
    ).fetchone()
    assert row == ("analyst_au", "test.event", {"question": "q", "nodes": ["a"]})
    for sql in [
        f"update audit.events set principal = 'x' where event_id = {event_id}",
        f"delete from audit.events where event_id = {event_id}",
        "truncate audit.events",
    ]:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            owner.execute(sql)
    assert _one(roles["admin"], "select count(*) from audit.events") >= 1
    _denied(roles["marketing"], "select count(*) from audit.events")


# -- adapter -------------------------------------------------------------------------------------


@pytest.mark.integration
def test_postgres_adapter(roles: dict[str, psycopg.Connection[Any]]) -> None:
    artifact = build(DbtArtifacts(DEMO / "artifacts"), HashEmbedder())
    adapter = PostgresAdapter(artifact)
    s = adapter.session_as(Principal("analyst_au", ""))
    try:
        rows = adapter.execute(s, "select region_code, order_id from marts.fct_orders", 5, 10)
        assert rows.truncated and len(rows.rows) == 5
        assert {r[0] for r in rows.rows} == {"AU"}
        assert rows.stats().row_count == 5
        plan = adapter.explain(s, "select count(*) from marts.fct_orders")
        assert "marts_base.fct_orders" in plan.relations
        with pytest.raises(psycopg.errors.QueryCanceled):
            adapter.execute(s, "select pg_sleep(3)", 1, 1)
        with pytest.raises(DENIED):
            adapter.execute(s, "select annual_salary from marts.dim_employees", 1, 5)
    finally:
        s.close()
    fresh = adapter.freshness("source.shop.orders")
    assert fresh.status in {"pass", "warn", "error"} and fresh.age_hours is not None
    assert fresh.warn_after_hours == 12.0


# -- the Phase 3 gate ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_graph() -> GraphArtifact:
    if not (DEMO / "artifacts" / "manifest.json").exists():
        pytest.skip("no demo/artifacts; run `make warehouse`")
    return build(
        DbtArtifacts(DEMO / "artifacts"),
        HashEmbedder(),
        caveats=DEMO / "caveats.yml",
        query_examples=DEMO / "query_examples.yml",
    )


def test_gate_description_lint_is_clean(real_graph: GraphArtifact) -> None:
    assert lint_descriptions(resolve(load_policies(POLICIES), real_graph), real_graph) == []


def test_gate_marketing_sees_no_email_or_salary_through_any_mcp_tool(
    real_graph: GraphArtifact,
) -> None:
    server = create_server(
        real_graph,
        load_policies(POLICIES),
        identity="marketing@kiwiretail.example",
        embedder=HashEmbedder(),
    )
    forbidden = re.compile(r"\.(email|work_email|phone|annual_salary)\b")

    async def sweep(c: Client) -> list[str]:
        outputs: list[str] = []

        async def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
            r = await c.call_tool(tool, args)
            first = r.content[0] if r.content else None
            text = (
                _err(first)
                if r.is_error and isinstance(first, TextContent)
                else json.dumps(r.structured_content)
            )
            outputs.append(text)
            return r.structured_content or {}

        for q in ["email", "customer email address", "phone", "salary", "annual salary pay"]:
            await call("search_nodes", {"query": q, "k": 50})
        for node in [
            "model.dim_customers",
            "model.dim_employees",
            "column.dim_customers.email",
            "column.dim_employees.annual_salary",
            "source.shop.customers",
        ]:
            await call("get_node", {"id": node})
            for level in ["model", "column"]:
                await call(
                    "get_lineage", {"id": node, "direction": "both", "depth": 10, "level": level}
                )
            await call("get_blast_radius", {"id": node})
        for metric in ["net_revenue", "churn_rate", "revenue"]:
            await call("get_metric", {"name": metric})
        return outputs

    outputs = asyncio.run(_with_client(server, sweep))
    # A refused id is echoed back in the error, exactly as for an id that never existed.
    errors = [o for o in outputs if o.startswith("Error executing tool")]
    assert all("unknown node id" in e for e in errors)
    leaks = [o for o in outputs if o not in errors and forbidden.search(o)]
    assert not leaks, leaks[0][:300]
    assert len(outputs) - len(errors) >= 15  # the sweep really exercised the tools
    assert len(errors) == 12  # 3 hidden ids x get_node, 2 lineage levels, blast radius


def _err(content: TextContent) -> str:
    return content.text


async def _with_client(server: Any, fn: Any) -> Any:
    async with Client(server) as client:
        return await fn(client)


@pytest.mark.integration
def test_gate_marketing_sees_no_email_or_salary_by_direct_query(
    roles: dict[str, psycopg.Connection[Any]],
) -> None:
    conn = roles["marketing"]
    for sql in [
        "select email from marts.dim_customers",
        "select annual_salary from marts.dim_employees",
        "select max(annual_salary) from marts.dim_employees",
        "select count(*) from marts.dim_customers where email like '%@%'",
        "select email from marts_base.dim_customers",
        "select email from staging.stg_shop__customers",
        "select email from raw.customers",
    ]:
        _denied(conn, sql)
