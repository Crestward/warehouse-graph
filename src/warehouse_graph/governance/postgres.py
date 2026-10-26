"""Compile a `PolicySpec` to Postgres (task 3.3). Pure: returns SQL, runs nothing.

Layout after `warehouse-graph policies` (design decision D13):

    marts_base.<t>   the tables dbt built, moved out of `marts`. RLS on, row predicates keyed
                     on current_user. No principal has USAGE on this schema.
    marts.<t>        a security_barrier view per table, owned by `policy_owner`, that masks
                     columns per principal. Principals get column-level SELECT on these views,
                     so a hidden column is never granted.
    raw/staging/intermediate   column-level grants only (no masking there: masked = hidden)
    audit.events     append-only (trigger), written by `audit_writer` (INSERT only)

Each principal is its own LOGIN role (NOINHERIT, read-only, statement timeout), so generated
SQL can't `SET ROLE` its way to more access. Generated SQL stays `marts.<t>` for every role and
every backend; the database decides what each role gets back.

The script is idempotent: rerun it after every `dbt build` (which replaces the marts tables and
drops the views, so access fails closed until the policies are reapplied).
"""

from __future__ import annotations

import os

from warehouse_graph.governance.policies import VIEW_SCHEMAS, PolicySpec, Relation

BASE_SUFFIX = "_base"
POLICY_OWNER = "policy_owner"
AUDIT_WRITER = "audit_writer"
STATEMENT_TIMEOUT = "30s"

MASK_FUNCTIONS = {
    "email": """create or replace function policy.mask_email(v text) returns text
language sql immutable parallel safe as $$
  select case when v is null then null
              else left(v, 1) || '***@***' || coalesce(substring(v from '\\.[A-Za-z]+$'), '') end
$$""",
    "phone": """create or replace function policy.mask_phone(v text) returns text
language sql immutable parallel safe as $$
  select case when v is null then null
              else '***-***-' || right(regexp_replace(v, '\\D', '', 'g'), 3) end
$$""",
    "redact": """create or replace function policy.mask_redact(v text) returns text
language sql immutable parallel safe as $$
  select case when v is null then null else '***' end
$$""",
    "initial": """create or replace function policy.mask_initial(v text) returns text
language sql immutable parallel safe as $$
  select case when v is null then null else left(v, 1) || '***' end
$$""",
}


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def role_password(role: str) -> str:
    """Login password for a principal's role: POSTGRES_PASSWORD_<ROLE>, else a local default."""
    return os.environ.get(f"POSTGRES_PASSWORD_{role.upper()}", f"local-dev-only-{role}")


def _qualified(schema: str, table: str) -> str:
    return f"{ident(schema)}.{ident(table)}"


def _ensure_role(role: str, login: bool, read_only: bool = True) -> list[str]:
    create = f"create role {ident(role)} {'login' if login else 'nologin'}"
    stmts = [
        "do $$ begin if not exists (select from pg_roles where rolname = "
        f"{lit(role)}) then {create}; end if; end $$"
    ]
    if login:
        stmts += [
            f"alter role {ident(role)} with login noinherit nosuperuser nocreatedb nocreaterole"
            f" noreplication nobypassrls connection limit 20 password {lit(role_password(role))}",
            f"alter role {ident(role)} set statement_timeout = {lit(STATEMENT_TIMEOUT)}",
            f"alter role {ident(role)} set default_transaction_read_only ="
            f" {'on' if read_only else 'off'}",
        ]
    return stmts


def _audit() -> list[str]:
    return [
        """create table if not exists audit.events (
  event_id bigint generated always as identity primary key,
  occurred_at timestamptz not null default clock_timestamp(),
  principal text not null,
  event_type text not null check (event_type <> ''),
  request_id text,
  payload jsonb not null default '{}'::jsonb
)""",
        """create or replace function audit.reject_change() returns trigger
language plpgsql as $$ begin raise exception 'audit.events is append-only'; end $$""",
        "drop trigger if exists events_append_only on audit.events",
        "create trigger events_append_only before update or delete on audit.events"
        " for each row execute function audit.reject_change()",
        "drop trigger if exists events_no_truncate on audit.events",
        "create trigger events_no_truncate before truncate on audit.events"
        " for each statement execute function audit.reject_change()",
        "revoke all on audit.events from public",
        f"grant usage on schema audit to {ident(AUDIT_WRITER)}",
        f"grant insert, select (event_id) on audit.events to {ident(AUDIT_WRITER)}",
    ]


def _row_predicate(spec: PolicySpec, rel: Relation) -> str:
    whens = [
        f"when {lit(name)} then {ident(a.row_filter.column)} = {lit(a.row_filter.equals)}"
        for name, a in spec.access.items()
        if a.row_filter and a.row_filter.column in rel.columns
    ]
    return f"case current_user {' '.join(whens)} else true end" if whens else "true"


def _view(spec: PolicySpec, rel: Relation) -> str:
    base = _qualified(rel.schema + BASE_SUFFIX, rel.table)
    short = rel.node_id.split(".", 1)[1]
    select: list[str] = []
    for col in rel.columns:
        cid = f"column.{short}.{col}"
        masked_for: dict[str, list[str]] = {}
        for name, a in spec.access.items():
            if cid in a.masked_columns:
                masked_for.setdefault(a.masked_columns[cid], []).append(name)
        expr = ident(col)
        for mask, roles in sorted(masked_for.items()):
            in_list = ", ".join(lit(r) for r in sorted(roles))
            expr = (
                f"case when current_user in ({in_list}) then policy.mask_{mask}({ident(col)})"
                f" else {expr} end"
            )
        select.append(f"  {expr} as {ident(col)}" if expr != ident(col) else f"  {expr}")
    body = ",\n".join(select)
    return (
        f"create view {_qualified(rel.schema, rel.table)} with (security_barrier) as\n"
        f"select\n{body}\nfrom {base}"
    )


def compile_postgres(spec: PolicySpec) -> list[str]:
    """Every statement needed to make the database match the spec, in order."""
    principals = sorted(spec.access)
    schemas = sorted({r.schema for r in spec.relations.values()})
    base_schemas = sorted(f"{s}{BASE_SUFFIX}" for s in VIEW_SCHEMAS if s in schemas)
    stmts: list[str] = [
        "create schema if not exists policy",
        "create schema if not exists audit",
        *(f"create schema if not exists {ident(s)}" for s in base_schemas),
        *(MASK_FUNCTIONS[m] for m in sorted(set(spec.policies.masks.values()))),
        *_ensure_role(POLICY_OWNER, login=False),
        *_ensure_role(AUDIT_WRITER, login=True, read_only=False),  # INSERT-only grant instead
        *(s for p in principals for s in _ensure_role(p, login=True)),
        *_audit(),
        f"grant usage on schema policy to {ident(POLICY_OWNER)}",
    ]

    # Tables in view-served schemas move behind policy views.
    for rel in sorted(spec.relations.values(), key=lambda r: (r.schema, r.table)):
        if rel.schema not in VIEW_SCHEMAS:
            continue
        base_schema = rel.schema + BASE_SUFFIX
        base = _qualified(base_schema, rel.table)
        stmts += [
            "do $$ begin if exists (select from pg_class c join pg_namespace n"
            " on n.oid = c.relnamespace"
            f" where n.nspname = {lit(rel.schema)} and c.relname = {lit(rel.table)}"
            " and c.relkind in ('r', 'p')) then"
            f" drop table if exists {base} cascade;"
            f" alter table {_qualified(rel.schema, rel.table)} set schema {ident(base_schema)};"
            " end if; end $$",
            f"alter table {base} enable row level security",
            f"drop policy if exists wg_rows on {base}",
            f"create policy wg_rows on {base} for select to {ident(POLICY_OWNER)}"
            f" using ({_row_predicate(spec, rel)})",
            f"drop view if exists {_qualified(rel.schema, rel.table)}",
            _view(spec, rel),
            f"alter view {_qualified(rel.schema, rel.table)} owner to {ident(POLICY_OWNER)}",
        ]
    for s in base_schemas:
        stmts += [
            f"grant usage on schema {ident(s)} to {ident(POLICY_OWNER)}",
            f"grant select on all tables in schema {ident(s)} to {ident(POLICY_OWNER)}",
        ]

    # Start every principal from nothing, then grant exactly what the spec allows.
    all_schemas = ", ".join(ident(s) for s in [*schemas, *base_schemas, "audit", "policy"])
    for p in principals:
        stmts += [
            f"revoke all on all tables in schema {all_schemas} from {ident(p)}",
            f"revoke all on schema {all_schemas} from {ident(p)}",
        ]
        access = spec.access[p]
        for s in sorted({r.schema for r in access.relations.values()}):
            stmts.append(f"grant usage on schema {ident(s)} to {ident(p)}")
        for rel in sorted(access.relations.values(), key=lambda r: (r.schema, r.table)):
            cols = ", ".join(ident(c) for c in access.visible_columns(rel))
            if cols:
                stmts.append(
                    f"grant select ({cols}) on {_qualified(rel.schema, rel.table)} to {ident(p)}"
                )
    for reader in spec.policies.audit_readers:
        stmts += [
            f"grant usage on schema audit to {ident(reader)}",
            f"grant select on audit.events to {ident(reader)}",
        ]
    return stmts


def redact(stmt: str) -> str:
    """Hide role passwords when printing the script."""
    head, sep, _ = stmt.partition(" password ")
    return f"{head}{sep}'***'" if sep else stmt
