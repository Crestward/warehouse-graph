"""Compile a `PolicySpec` to Unity Catalog (task 9.4). Pure: returns SQL, runs nothing.

Unity Catalog has native row filters and column masks, but no column-level grants, so a hidden
column can't simply be left out of a grant. Layout (design decision D17):

    <catalog>.marts.<t>              the tables dbt built, with a row filter and column masks
                                     keyed on current_user(); no principal is granted them
    <catalog>_<principal>.<schema>   one catalog of views per principal: visible columns only,
                                     masked columns masked, rows filtered. A principal is granted
                                     only its own catalog.
    <catalog>.policy                 mask functions, principal() (user -> principal), filters

A session's default catalog is its principal's, so generated SQL stays `marts.<t>` for every role
and every backend. The views and the native filters are two independent layers: either one alone
gives the right rows and masks. That matters between a dbt build (which replaces the tables, and
with them the filters) and the next `policies` run: the views still filter.

Raw, staging and intermediate tables carry no filter, so the fault injector can write to raw
(filtered tables don't accept every write; PRD §6.4).

The script is idempotent. A principal without a user (`identities`) gets its views but no grants.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from warehouse_graph.governance.policies import VIEW_SCHEMAS, Access, PolicySpec, Relation

# (schema, table, column) -> type, as Unity Catalog reports it
ColumnTypes = Mapping[tuple[str, str, str], str]

MASK_FUNCTIONS = {
    "email": "case when v is null then null else concat(left(v, 1), '***@***',"
    " regexp_extract(v, '([.][A-Za-z]+)$', 1)) end",
    "phone": "case when v is null then null else"
    " concat('***-***-', right(regexp_replace(v, '[^0-9]', ''), 3)) end",
    "redact": "case when v is null then null else '***' end",
    "initial": "case when v is null then null else concat(left(v, 1), '***') end",
}


def ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def lit(value: str) -> str:
    if "\\" in value:
        raise ValueError(f"unsupported character in literal {value!r}")
    return "'" + value.replace("'", "''") + "'"


def principal_catalog(catalog: str, principal: str) -> str:
    name = f"{catalog}_{principal}"
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"unsafe catalog name {name!r}")
    return name


def _table(catalog: str, rel: Relation) -> str:
    return f"{ident(catalog)}.{ident(rel.schema)}.{ident(rel.table)}"


def _fn(catalog: str, name: str) -> str:
    return f"{ident(catalog)}.`policy`.{ident(name)}"


def _column_id(rel: Relation, column: str) -> str:
    return f"column.{rel.node_id.split('.', 1)[1]}.{column}"


def _row_filter(spec: PolicySpec, rel: Relation) -> tuple[str | None, str]:
    """(filter column, boolean expression on principal()) for a table's native row filter.
    Deny by default: an unknown user, or a principal that can't see the table, gets no rows."""
    column: str | None = None
    whens: list[str] = []
    for name, access in sorted(spec.access.items()):
        if rel.node_id not in access.relations:
            continue
        rf = access.row_filter
        if rf is not None and rf.column in rel.columns:
            column = rf.column
            whens.append(f"when {lit(name)} then {ident(rf.column)} = {lit(rf.equals)}")
        else:
            whens.append(f"when {lit(name)} then true")
    return column, (whens and " ".join(whens)) or ""


def _mask_case(spec: PolicySpec, catalog: str, rel: Relation, column: str, type_: str) -> str:
    """The native mask of one column: per principal, the value, its mask, or NULL if hidden."""
    cid = _column_id(rel, column)
    whens: list[str] = []
    for name, access in sorted(spec.access.items()):
        if rel.node_id not in access.relations or cid in access.hidden_columns:
            continue  # falls through to NULL
        if cid in access.masked_columns:
            if type_.lower() != "string":
                raise ValueError(f"{cid} is {type_}; only string columns can be masked")
            whens.append(
                f"when {lit(name)} then {_fn(catalog, 'mask_' + access.masked_columns[cid])}(v)"
            )
        else:
            whens.append(f"when {lit(name)} then v")
    body = " ".join(whens)
    return f"case {_fn(catalog, 'principal')}() {body} else null end" if whens else "null"


def _view(catalog: str, principal: str, access: Access, rel: Relation) -> str | None:
    select: list[str] = []
    for column in access.visible_columns(rel):
        mask = access.masked_columns.get(_column_id(rel, column))
        expr = ident(column)
        if mask:
            expr = f"{_fn(catalog, 'mask_' + mask)}({expr}) as {expr}"
        select.append(expr)
    if not select:
        return None
    rf = access.row_filter
    where = ""
    if rf is not None and rf.column in rel.columns:
        where = f" where {ident(rf.column)} = {lit(rf.equals)}"
    target = _table(principal_catalog(catalog, principal), rel)
    return (
        f"create or replace view {target} as select {', '.join(select)}"
        f" from {_table(catalog, rel)}{where}"
    )


def compile_unity(
    spec: PolicySpec, catalog: str, types: ColumnTypes, identities: Mapping[str, str]
) -> list[str]:
    """Every statement needed to make Unity Catalog match the spec, in order.

    `identities` maps a principal to the Unity Catalog user (or service principal) it logs in as.
    """
    unknown = set(identities) - set(spec.access)
    if unknown:
        raise ValueError(f"identities for unknown principals: {sorted(unknown)}")
    relations = sorted(spec.relations.values(), key=lambda r: (r.schema, r.table))
    users = {p: u for p, u in sorted(identities.items())}
    if len(set(users.values())) != len(users):
        raise ValueError("one user can't be two principals")
    whens = " ".join(f"when {lit(u)} then {lit(p)}" for p, u in users.items())
    stmts = [
        f"create schema if not exists {ident(catalog)}.`policy`",
        f"create or replace function {_fn(catalog, 'principal')}() returns string"
        f" return {f'case current_user() {whens} else null end' if whens else 'null'}",
        *(
            f"create or replace function {_fn(catalog, 'mask_' + m)}(v string) returns string"
            f" return {MASK_FUNCTIONS[m]}"
            for m in sorted(set(spec.policies.masks.values()))
        ),
    ]
    stmts += _native_controls(spec, catalog, relations, types)
    for principal, access in sorted(spec.access.items()):
        stmts += _principal_catalog(catalog, principal, access)
    # Grants last: until here, a new principal can see nothing.
    for principal, user in users.items():
        stmts += _grants(catalog, principal, user, spec)
    return stmts


def _native_controls(
    spec: PolicySpec, catalog: str, relations: list[Relation], types: ColumnTypes
) -> list[str]:
    stmts: list[str] = []
    for rel in relations:
        if rel.schema not in VIEW_SCHEMAS:
            continue
        table = _table(catalog, rel)
        column, whens = _row_filter(spec, rel)
        name = f"rows_{rel.table}"
        args = ""
        if column is not None:
            args = f"{ident(column)} {types[(rel.schema, rel.table, column)]}"
        body = f"case {_fn(catalog, 'principal')}() {whens} else false end" if whens else "false"
        stmts += [
            # A function in use can't be replaced: detach, replace, attach.
            f"alter table {table} drop row filter",
            f"create or replace function {_fn(catalog, name)}({args}) returns boolean"
            f" return {body}",
            f"alter table {table} set row filter {_fn(catalog, name)}"
            f" on ({ident(column) if column else ''})",
        ]
        for col in rel.columns:
            cid = _column_id(rel, col)
            restricted = any(
                cid in a.hidden_columns or cid in a.masked_columns for a in spec.access.values()
            )
            if not restricted:
                continue
            type_ = types[(rel.schema, rel.table, col)]
            fn = _fn(catalog, f"col_{rel.table}__{col}")
            stmts += [
                f"alter table {table} alter column {ident(col)} drop mask",
                f"create or replace function {fn}(v {type_}) returns {type_}"
                f" return {_mask_case(spec, catalog, rel, col, type_)}",
                f"alter table {table} alter column {ident(col)} set mask {fn}",
            ]
    return stmts


def _principal_catalog(catalog: str, principal: str, access: Access) -> list[str]:
    own = principal_catalog(catalog, principal)
    visible = sorted(access.relations.values(), key=lambda r: (r.schema, r.table))
    stmts = [
        # Start from nothing, so a relation the policy dropped disappears.
        f"drop catalog if exists {ident(own)} cascade",
        f"create catalog {ident(own)}",
        f"drop schema if exists {ident(own)}.`default` cascade",
        *(
            f"create schema {ident(own)}.{ident(s)}"
            for s in sorted({rel.schema for rel in visible})
        ),
    ]
    for rel in visible:
        view = _view(catalog, principal, access, rel)
        if view is not None:
            stmts.append(view)
    return stmts


def _grants(catalog: str, principal: str, user: str, spec: PolicySpec) -> list[str]:
    own = ident(principal_catalog(catalog, principal))
    stmts = [f"revoke all privileges on catalog {ident(catalog)} from {ident(user)}"]
    for other in sorted(spec.access):
        if other != principal:
            other_catalog = ident(principal_catalog(catalog, other))
            stmts.append(f"revoke all privileges on catalog {other_catalog} from {ident(user)}")
    stmts += [
        f"grant use catalog, use schema, select on catalog {own} to {ident(user)}",
        # Row filters and masks on the tables behind the views are evaluated as the caller.
        f"grant use catalog on catalog {ident(catalog)} to {ident(user)}",
        f"grant use schema, execute on schema {ident(catalog)}.`policy` to {ident(user)}",
    ]
    return stmts
