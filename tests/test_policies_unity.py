"""Unity Catalog policies (task 9.4-9.5).

The compiler tests are offline. The `databricks` tests compare the live workspace with local
Postgres, principal by principal: the same policies.yml must give the same access on both. They
need DATABRICKS_* settings, the demo on both backends and WG_DATABRICKS_TESTS=1 (they use
workspace compute, and one of them remaps the deployer's identity for a few minutes).

On a workspace with one user, two things are tested separately: the principals' views (through
their catalogs) and the native row filters and masks (by mapping the deployer to each principal
in turn). Grants are what a single owner can't test; `spike-databricks.md` check 3.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from warehouse_graph.adapters import (
    AccessDenied,
    PostgresAdapter,
    UnknownObject,
    WarehouseError,
)
from warehouse_graph.adapters.databricks import (
    DatabricksAdapter,
    NoIdentityError,
    to_databricks,
    translate,
)
from warehouse_graph.cli import load_dotenv
from warehouse_graph.governance import Principal, load_policies, resolve
from warehouse_graph.governance.policies import VIEW_SCHEMAS, PolicySpec, Relation
from warehouse_graph.governance.unity import compile_unity, ident, principal_catalog
from warehouse_graph.graph import DbtArtifacts, GraphArtifact, build
from warehouse_graph.graph.embed import HashEmbedder

ROOT = Path(__file__).resolve().parents[1]
PRINCIPALS = ["admin", "finance", "marketing", "analyst_au", "analyst_nz", "sentinel"]
USERS = {"admin": "deployer@example.com", "analyst_au": "au@example.com"}


@pytest.fixture(scope="module")
def spec() -> PolicySpec:
    artifacts = ROOT / "demo" / "artifacts"
    if not (artifacts / "manifest.json").exists():
        pytest.skip("no demo artifacts; run `make warehouse`")
    graph = build(DbtArtifacts(artifacts), HashEmbedder())
    return resolve(load_policies(ROOT / "policies.yml"), graph)


@pytest.fixture(scope="module")
def script(spec: PolicySpec) -> list[str]:
    types = {(r.schema, r.table, c): "string" for r in spec.relations.values() for c in r.columns}
    for schema, table, column in list(types):
        if column == "annual_salary":
            types[(schema, table, column)] = "decimal(12,2)"
    return compile_unity(spec, "kiwi", types, USERS)


# -- compiler (offline) --------------------------------------------------------------------------


def _one(script: list[str], *parts: str) -> str:
    [found] = [s for s in script if all(p in s for p in parts)]
    return found


def test_every_principal_gets_only_its_own_catalog(script: list[str]) -> None:
    grants = [s for s in script if s.startswith("grant ") and "au@example.com" in s]
    assert grants == [
        "grant use catalog, use schema, select on catalog `kiwi_analyst_au` to `au@example.com`",
        "grant use catalog on catalog `kiwi` to `au@example.com`",
        "grant use schema, execute on schema `kiwi`.`policy` to `au@example.com`",
    ]
    revoked = [s for s in script if s.startswith("revoke ") and "au@example.com" in s]
    assert len(revoked) == len(PRINCIPALS)  # the base catalog and the five other principals'
    assert not any("finance" in s and s.startswith("grant") for s in script)  # no user, no grant


def test_views_drop_hidden_columns_mask_and_filter(script: list[str]) -> None:
    marketing = _one(script, "create or replace view `kiwi_marketing`.`marts`.`dim_customers`")
    assert "email" not in marketing and "phone" not in marketing
    assert "`mask_initial`(`first_name`) as `first_name`" in marketing
    au = _one(script, "create or replace view `kiwi_analyst_au`.`marts`.`dim_customers`")
    assert au.endswith("where `region_code` = 'AU'") and "`mask_email`(`email`)" in au
    assert not any("`kiwi_analyst_au`.`marts`.`orders_v1`" in s for s in script)  # can't filter it
    assert not any("`kiwi_analyst_au`.`raw`" in s for s in script)


def test_native_row_filter_denies_unknown_users(script: list[str]) -> None:
    body = _one(script, "create or replace function `kiwi`.`policy`.`rows_fct_orders`")
    assert "when 'analyst_au' then `region_code` = 'AU'" in body
    assert "when 'finance' then true" in body and body.endswith("else false end")
    legacy = _one(script, "create or replace function `kiwi`.`policy`.`rows_orders_v1`")
    assert "analyst" not in legacy  # falls through to false
    who = _one(script, "create or replace function `kiwi`.`policy`.`principal`")
    assert "when 'au@example.com' then 'analyst_au'" in who and who.endswith("else null end")


def test_native_masks_null_hidden_columns(script: list[str]) -> None:
    email = _one(script, "function `kiwi`.`policy`.`col_dim_customers__email`")
    assert "when 'marketing'" not in email and "else null end" in email
    assert "when 'finance' then `kiwi`.`policy`.`mask_email`(v)" in email
    salary = _one(script, "function `kiwi`.`policy`.`col_dim_employees__annual_salary`")
    assert "(v decimal(12,2)) returns decimal(12,2)" in salary and "'analyst_au'" not in salary


def test_raw_tables_stay_unfiltered(script: list[str]) -> None:
    assert not any("set row filter" in s and "`raw`" in s for s in script)


def test_grants_come_last_and_one_user_is_one_principal(spec: PolicySpec) -> None:
    types = {(r.schema, r.table, c): "string" for r in spec.relations.values() for c in r.columns}
    script = compile_unity(spec, "kiwi", types, {})
    assert not any(s.startswith("grant") for s in script)
    with pytest.raises(ValueError, match="two principals"):
        compile_unity(spec, "kiwi", types, {"admin": "a@x", "finance": "a@x"})
    with pytest.raises(ValueError, match="unknown principals"):
        compile_unity(spec, "kiwi", types, {"nobody": "a@x"})


# -- adapter (offline) ---------------------------------------------------------------------------


def test_sql_is_transpiled_and_kept_to_one_query() -> None:
    sql = to_databricks("select date_trunc('month', d)::date as m, sum(x) from marts.t group by 1")
    assert "marts.t" in sql and "::" not in sql
    for bad in (
        "select 1; select 2",
        "delete from marts.t",
        "set role admin",
        "drop table marts.t",
    ):
        with pytest.raises(WarehouseError):
            to_databricks(bad)


def test_the_base_catalog_cannot_be_named() -> None:
    for sql in ("select * from kiwi.marts.dim_customers", "select * from kiwi_admin.marts.t"):
        with pytest.raises(AccessDenied):
            to_databricks(sql)


def test_errors_are_translated() -> None:
    assert isinstance(translate(RuntimeError("[INSUFFICIENT_PERMISSIONS] no")), AccessDenied)
    assert isinstance(translate(RuntimeError("[UNRESOLVED_COLUMN] x")), UnknownObject)
    assert isinstance(translate(RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] x\nat y")), UnknownObject)
    assert type(translate(RuntimeError("[DIVIDE_BY_ZERO] x"))) is WarehouseError


def test_no_login_means_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABRICKS_TOKEN_FINANCE", raising=False)
    with pytest.raises(NoIdentityError):
        DatabricksAdapter().session_as(Principal("finance", ""))


# -- live: the same access on both backends ------------------------------------------------------

live = pytest.mark.databricks


@pytest.fixture(scope="module")
def unity() -> DatabricksAdapter:
    load_dotenv(ROOT / ".env")
    if os.environ.get("WG_DATABRICKS_TESTS") != "1" or "DATABRICKS_TOKEN" not in os.environ:
        pytest.skip("set WG_DATABRICKS_TESTS=1 (and DATABRICKS_*) to test the live workspace")
    return DatabricksAdapter(single_identity=True)


@pytest.fixture(scope="module")
def live_spec() -> PolicySpec:
    path = ROOT / "build" / "graph.wgz"
    if not path.exists():
        pytest.skip("no build/graph.wgz")
    return resolve(load_policies(ROOT / "policies.yml"), GraphArtifact.load(path))


@pytest.fixture(scope="module")
def postgres() -> PostgresAdapter:
    adapter = PostgresAdapter()
    try:
        adapter.session_as(Principal("admin", "")).close()
    except WarehouseError:
        pytest.skip("local policy roles missing; run `make policies`")
    return adapter


def _relations(spec: PolicySpec, principal: str) -> list[Relation]:
    return sorted(spec.access[principal].relations.values(), key=lambda r: (r.schema, r.table))


def _counts_sql(relations: list[Relation]) -> str:
    return " union all ".join(
        f"select '{r.schema}.{r.table}' as t, count(*) as n from {r.schema}.{r.table}"
        for r in relations
    )


def _probe_sql(rel: Relation, columns: list[str], prefix: str = "") -> str:
    """Per column: how many non-null and distinct values, and the smallest (as text)."""
    parts = []
    for c in columns:
        parts += [f"count({c})", f"count(distinct {c})"]
    return f"select count(*), {', '.join(parts)} from {prefix}{rel.schema}.{rel.table}"


@live
@pytest.mark.parametrize("principal", PRINCIPALS)
def test_views_show_the_same_rows_and_columns_as_postgres(
    unity: DatabricksAdapter, postgres: PostgresAdapter, live_spec: PolicySpec, principal: str
) -> None:
    access = live_spec.access[principal]
    relations = _relations(live_spec, principal)
    pg = postgres.session_as(Principal(principal, ""))
    uc = unity.session_as(Principal(principal, ""))
    try:
        sql = _counts_sql(relations)
        assert sorted(unity.execute(uc, sql, 100, 120).rows) == sorted(
            postgres.execute(pg, sql, 100, 60).rows
        )
        for rel in relations:
            if rel.schema not in VIEW_SCHEMAS:
                continue
            probe = _probe_sql(rel, access.visible_columns(rel))
            assert (
                unity.execute(uc, probe, 1, 120).rows == postgres.execute(pg, probe, 1, 60).rows
            ), f"{principal}: {rel.schema}.{rel.table}"
    finally:
        pg.close()
        uc.close()


@live
@pytest.mark.parametrize("principal", PRINCIPALS)
def test_masked_values_are_identical_on_both_backends(
    unity: DatabricksAdapter, postgres: PostgresAdapter, live_spec: PolicySpec, principal: str
) -> None:
    access = live_spec.access[principal]
    pg = postgres.session_as(Principal(principal, ""))
    uc = unity.session_as(Principal(principal, ""))
    try:
        for rel in _relations(live_spec, principal):
            masked = [
                c
                for c in rel.columns
                if f"column.{rel.node_id.split('.', 1)[1]}.{c}" in access.masked_columns
            ]
            for c in masked:
                sql = f"select {c}, count(*) from {rel.schema}.{rel.table} group by {c}"
                a = unity.execute(uc, sql, 5000, 120).rows
                b = postgres.execute(pg, sql, 5000, 60).rows
                assert sorted(map(repr, a)) == sorted(map(repr, b)), f"{principal}: {rel.table}.{c}"
                if principal != "admin" and a:
                    assert all(v is None or "***" in str(v) for v, _ in a)
    finally:
        pg.close()
        uc.close()


@live
@pytest.mark.parametrize("principal", PRINCIPALS)
def test_hidden_columns_and_tables_do_not_exist_for_the_principal(
    unity: DatabricksAdapter, live_spec: PolicySpec, principal: str
) -> None:
    access = live_spec.access[principal]
    uc = unity.session_as(Principal(principal, ""))
    try:
        for cid in sorted(access.hidden_columns):
            rel = next(
                r for r in access.relations.values() if cid.startswith(
                    f"column.{r.node_id.split('.', 1)[1]}."
                ) and cid.rsplit(".", 1)[1] in r.columns
            )  # fmt: skip
            with pytest.raises(UnknownObject):
                unity.execute(
                    uc, f"select {cid.rsplit('.', 1)[1]} from {rel.schema}.{rel.table}", 1, 60
                )
        unseen = [r for k, r in live_spec.relations.items() if k not in access.relations]
        for rel in unseen[:12]:
            with pytest.raises(UnknownObject):
                unity.execute(uc, f"select count(*) from {rel.schema}.{rel.table}", 1, 60)
    finally:
        uc.close()


@live
def test_principals_have_no_grants_beyond_their_own_catalog(unity: DatabricksAdapter) -> None:
    from warehouse_graph.adapters.databricks import connect

    owner = unity.current_user()
    with connect(os.environ["DATABRICKS_TOKEN"]) as conn, conn.cursor() as cur:
        for c in [unity.catalog, *(principal_catalog(unity.catalog, p) for p in PRINCIPALS)]:
            cur.execute(f"show grants on catalog {ident(c)}")
            grantees = {r[0] for r in cur.fetchall()} - {owner}
            assert grantees <= set(
                filter(None, (os.environ.get(f"DATABRICKS_USER_{p.upper()}") for p in PRINCIPALS))
            ), f"{c}: unexpected grantees"


@pytest.fixture
def remap(unity: DatabricksAdapter) -> Iterator[Any]:
    """Make the deployer count as another principal, for the native filters. Always restored."""
    user = unity.current_user()

    def as_principal(principal: str | None) -> None:
        value = "null" if principal is None else f"'{principal}'"
        unity.run_as_deployer(
            [
                f"create or replace function {ident(unity.catalog)}.`policy`.`principal`()"
                f" returns string return case current_user() when '{user}' then {value}"
                " else null end"
            ]
        )

    yield as_principal
    as_principal("admin")


@live
def test_native_filters_and_masks_alone_give_the_same_access(
    unity: DatabricksAdapter, postgres: PostgresAdapter, live_spec: PolicySpec, remap: Any
) -> None:
    """The second layer: the base tables themselves, with no view in the way."""
    from warehouse_graph.adapters.databricks import connect

    for principal in PRINCIPALS:
        access = live_spec.access[principal]
        remap(principal)
        pg = postgres.session_as(Principal(principal, ""))
        try:
            with connect(os.environ["DATABRICKS_TOKEN"], unity.catalog) as conn:
                cur = conn.cursor()
                marts = sorted(
                    (r for r in live_spec.relations.values() if r.schema in VIEW_SCHEMAS),
                    key=lambda r: r.table,
                )
                cur.execute(_counts_sql(marts))
                got = {r[0]: r[1] for r in cur.fetchall()}
                visible = [r for r in marts if r.node_id in access.relations]
                want = {t: n for t, n in postgres.execute(pg, _counts_sql(visible), 100, 60).rows}
                for rel in marts:
                    name = f"{rel.schema}.{rel.table}"
                    assert got[name] == want.get(name, 0), f"{principal}: {name}"
                for rel in visible:
                    for c in rel.columns:
                        cid = f"column.{rel.node_id.split('.', 1)[1]}.{c}"
                        if cid in access.hidden_columns:
                            cur.execute(f"select count({c}) from {rel.schema}.{rel.table}")
                            assert cur.fetchone()[0] == 0, f"{principal}: {cid} is not NULL"
                        elif cid in access.masked_columns:
                            sql = f"select {c}, count(*) from {rel.schema}.{rel.table} group by {c}"
                            cur.execute(sql)
                            a = sorted(repr(tuple(r)) for r in cur.fetchall())
                            b = sorted(map(repr, postgres.execute(pg, sql, 5000, 60).rows))
                            assert a == b, f"{principal}: {cid}"
        finally:
            pg.close()
    remap(None)  # a user the policy doesn't know
    with connect(os.environ["DATABRICKS_TOKEN"], unity.catalog) as conn:
        cur = conn.cursor()
        cur.execute("select count(*) from marts.fct_orders")
        assert cur.fetchone()[0] == 0
