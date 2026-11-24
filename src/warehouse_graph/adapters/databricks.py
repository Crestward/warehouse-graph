"""`WarehouseAdapter` for the `databricks-dev` profile: Unity Catalog on a SQL warehouse (task 9.3).

SQL comes in as Postgres, like everywhere else, and is transpiled to Databricks with sqlglot, so
prompts, gold SQL and checks don't fork per backend. A session's default catalog is its
principal's catalog of views (governance/unity.py), so `marts.<t>` means what that principal may
see.

**Identity.** Each principal logs in as its own user: DATABRICKS_TOKEN_<PRINCIPAL>. Without one,
`session_as` refuses, unless `single_identity=True`: then it uses the deployer's own token with
the principal's catalog. That exercises the views, not the grants (the deployer owns everything),
and is only for testing on a workspace with one user (spike-databricks.md check 3).
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from warehouse_graph.adapters.protocols import (
    AccessDenied,
    Freshness,
    PlanInfo,
    Rows,
    Session,
    UnknownObject,
    WarehouseError,
)
from warehouse_graph.governance.policies import PolicySpec, Principal
from warehouse_graph.governance.unity import compile_unity, ident, principal_catalog
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import SourceNode

DEFAULT_CATALOG = "kiwi"
STATEMENT_TIMEOUT_S = 30
_DENIED = ("INSUFFICIENT_PERMISSIONS", "PERMISSION_DENIED", "INSUFFICIENT_PRIVILEGES")
_UNKNOWN = (
    "UNRESOLVED_COLUMN",
    "TABLE_OR_VIEW_NOT_FOUND",
    "SCHEMA_NOT_FOUND",
    "CATALOG_NOT_FOUND",
    "NO_SUCH_CATALOG_EXCEPTION",
    "UNRESOLVED_ROUTINE",
)


class NoIdentityError(RuntimeError):
    """No login is configured for a principal."""


def catalog_from_env() -> str:
    return os.environ.get("DATABRICKS_CATALOG", DEFAULT_CATALOG)


def identities_from_env(principals: Iterable[str]) -> dict[str, str]:
    """principal -> Unity Catalog user, from DATABRICKS_USER_<PRINCIPAL>."""
    found = {p: os.environ.get(f"DATABRICKS_USER_{p.upper()}", "") for p in principals}
    return {p: u for p, u in found.items() if u}


def _server() -> tuple[str, str]:
    host = os.environ["DATABRICKS_HOST"].removeprefix("https://").split("/", 1)[0]
    return host, os.environ["DATABRICKS_HTTP_PATH"]


def connect(token: str, catalog: str | None = None) -> Any:
    from databricks import sql  # optional dependency: warehouse-graph[databricks]

    host, http_path = _server()
    return sql.connect(
        server_hostname=host,
        http_path=http_path,
        access_token=token,
        catalog=catalog,
        schema="information_schema" if catalog else None,
        user_agent_entry="warehouse-graph",
        use_cloud_fetch=False,
    )


def translate(error: Exception) -> WarehouseError:
    """A connector error as one of the adapter's own, without its stack trace or request ids."""
    first = str(error).splitlines()[0][:300] if str(error) else type(error).__name__
    if any(code in first for code in _DENIED):
        return AccessDenied(first)
    if any(code in first for code in _UNKNOWN):
        return UnknownObject(first)
    return WarehouseError(first)


def to_databricks(sql: str) -> str:
    """One Postgres query -> Databricks SQL. Anything but a single query is refused, and so is a
    table qualified with a catalog: the session's catalog decides what `marts.<t>` is."""
    trees = [t for t in sqlglot.parse(sql, dialect="postgres") if t is not None]
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise WarehouseError("expected exactly one query")
    for table in trees[0].find_all(exp.Table):
        if table.catalog:
            raise AccessDenied(f"cross-catalog reference: {table.sql(dialect='postgres')}")
    for node in trees[0].find_all(exp.Into, exp.DML, exp.DDL):
        raise AccessDenied(f"not read-only: {type(node).__name__}")
    return trees[0].sql(dialect="databricks")


@dataclass
class DatabricksSession:
    principal: Principal
    conn: Any
    single_identity: bool

    def close(self) -> None:
        with suppress(Exception):
            self.conn.close()


def _conn(s: Session) -> Any:
    if not isinstance(s, DatabricksSession):
        raise TypeError("DatabricksAdapter needs a session from its own session_as()")
    return s.conn


class DatabricksAdapter:
    def __init__(
        self,
        graph: GraphArtifact | None = None,
        *,
        catalog: str | None = None,
        single_identity: bool = False,
    ) -> None:
        self.graph = graph
        self.catalog = catalog or catalog_from_env()
        self.single_identity = single_identity

    def _token(self, principal: Principal) -> tuple[str, bool]:
        own = os.environ.get(f"DATABRICKS_TOKEN_{principal.name.upper()}")
        if own:
            return own, False
        if not self.single_identity:
            raise NoIdentityError(
                f"no DATABRICKS_TOKEN_{principal.name.upper()}: refusing to run {principal.name}'s"
                " queries as someone else (single_identity=True to test with one user)"
            )
        return os.environ["DATABRICKS_TOKEN"], True

    def session_as(self, principal: Principal) -> DatabricksSession:
        token, shared = self._token(principal)
        conn = connect(token, principal_catalog(self.catalog, principal.name))
        with conn.cursor() as cur:
            cur.execute(f"set statement_timeout = {STATEMENT_TIMEOUT_S}")
        return DatabricksSession(principal, conn, shared)

    def explain(self, s: Session, sql: str) -> PlanInfo:
        query = to_databricks(sql)
        rows = self._run(s, f"explain {query}")
        text = "\n".join(str(r[0]) for r in rows)
        if "Error occurred during query planning" in text or "AnalysisException" in text:
            raise translate(RuntimeError(text.replace("== Physical Plan ==", "").strip()))
        tables = sqlglot.parse_one(query, dialect="databricks").find_all(exp.Table)
        return PlanInfo(
            total_cost=0.0,  # Databricks' EXPLAIN gives no cost
            estimated_rows=-1,
            relations=tuple(sorted({f"{t.db}.{t.name}" for t in tables if t.db})),
            plan={"text": text},
        )

    def execute(self, s: Session, sql: str, limit: int, timeout_s: int) -> Rows:
        query = to_databricks(sql)
        started = time.perf_counter()
        try:
            with _conn(s).cursor() as cur:
                cur.execute(f"set statement_timeout = {int(timeout_s)}")
                cur.execute(query)
                columns = tuple(d[0] for d in cur.description or ())
                fetched = cur.fetchmany(limit + 1)
        except Exception as e:
            raise translate(e) from None
        return Rows(
            columns=columns,
            rows=[tuple(r) for r in fetched[:limit]],
            truncated=len(fetched) > limit,
            elapsed_s=round(time.perf_counter() - started, 4),
        )

    def _run(self, s: Session, sql: str) -> list[Any]:
        try:
            with _conn(s).cursor() as cur:
                cur.execute(sql)
                return list(cur.fetchall())
        except Exception as e:
            raise translate(e) from None

    # -- deployer operations (the workspace owner's token) ---------------------------------------

    def column_types(self) -> dict[tuple[str, str, str], str]:
        with connect(os.environ["DATABRICKS_TOKEN"]) as conn, conn.cursor() as cur:
            cur.execute(
                "select table_schema, table_name, column_name, full_data_type"
                f" from {ident(self.catalog)}.information_schema.columns"
            )
            return {(r[0], r[1], r[2]): r[3] for r in cur.fetchall()}

    def apply_policies(
        self, policies: PolicySpec, identities: Mapping[str, str] | None = None
    ) -> None:
        if identities is None:
            identities = identities_from_env(policies.access)
        self.run_as_deployer(compile_unity(policies, self.catalog, self.column_types(), identities))

    def run_as_deployer(self, statements: list[str]) -> None:
        """In order, one connection. `drop row filter` / `drop mask` on a table without one, and
        revoking what was never granted, are no-ops."""
        with connect(os.environ["DATABRICKS_TOKEN"]) as conn, conn.cursor() as cur:
            for stmt in statements:
                try:
                    cur.execute(stmt)
                except Exception as e:
                    if stmt.startswith("alter table") and " drop " in stmt:
                        continue
                    raise translate(e) from None

    def current_user(self) -> str:
        with connect(os.environ["DATABRICKS_TOKEN"]) as conn, conn.cursor() as cur:
            cur.execute("select current_user()")
            return str(cur.fetchone()[0])

    def freshness(self, node_id: str) -> Freshness:
        if self.graph is None:
            raise ValueError("freshness needs the graph (DatabricksAdapter(graph=...))")
        node = self.graph.nodes.get(node_id)
        if not isinstance(node, SourceNode) or not node.loaded_at_field:
            raise ValueError(f"{node_id} is not a source with a loaded_at field")
        table = node.relation.replace('"', "").split(".")
        target = ".".join(ident(p) for p in (self.catalog, *table[-2:]))
        with connect(os.environ["DATABRICKS_TOKEN"]) as conn, conn.cursor() as cur:
            cur.execute(f"select max({ident(node.loaded_at_field)}) from {target}")
            loaded: dt.datetime | None = cur.fetchone()[0]
        age = (dt.datetime.now(dt.UTC) - loaded).total_seconds() / 3600 if loaded else None
        status: Any = "unknown"
        if age is not None:
            status = "pass"
            if node.freshness_warn_hours is not None and age > node.freshness_warn_hours:
                status = "warn"
            if node.freshness_error_hours is not None and age > node.freshness_error_hours:
                status = "error"
        return Freshness(
            node_id=node_id,
            max_loaded_at=loaded,
            age_hours=round(age, 2) if age is not None else None,
            warn_after_hours=node.freshness_warn_hours,
            error_after_hours=node.freshness_error_hours,
            status=status,
        )
