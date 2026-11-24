"""`WarehouseAdapter` for the local profile: Postgres 16 (task 3.1).

Each principal connects as its own login role (PRD §3.4), so nothing in the session can be
talked into more access: no SET ROLE target, read-only by default, statement timeout set on
the role. Policy application and freshness checks use the admin (warehouse owner) connection.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass
from typing import Any

import psycopg

from warehouse_graph.adapters.protocols import Freshness, PlanInfo, Rows, Session
from warehouse_graph.demo.load import conninfo_from_env
from warehouse_graph.governance.policies import PolicySpec, Principal
from warehouse_graph.governance.postgres import compile_postgres, role_password
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import SourceNode


def principal_conninfo(principal: Principal) -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=principal.name,
        password=role_password(principal.name),
        dbname=os.environ.get("POSTGRES_DB", "warehouse"),
        connect_timeout=10,
        application_name=f"warehouse-graph:{principal.name}",
    )


@dataclass
class PostgresSession:
    principal: Principal
    conn: psycopg.Connection[Any]

    def close(self) -> None:
        self.conn.close()


def _conn(s: Session) -> psycopg.Connection[Any]:
    if not isinstance(s, PostgresSession):
        raise TypeError("PostgresAdapter needs a session from its own session_as()")
    return s.conn


def _relations(plan: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    if "Relation Name" in plan:
        found.add(f"{plan.get('Schema', '')}.{plan['Relation Name']}")
    for child in plan.get("Plans", []):
        found |= _relations(child)
    return found


class PostgresAdapter:
    def __init__(self, graph: GraphArtifact | None = None, admin_conninfo: str | None = None):
        self.graph = graph
        self.admin_conninfo = admin_conninfo or conninfo_from_env()

    def session_as(self, principal: Principal) -> PostgresSession:
        return PostgresSession(principal, psycopg.connect(principal_conninfo(principal)))

    def explain(self, s: Session, sql: str) -> PlanInfo:
        conn = _conn(s)
        try:
            row = conn.execute(f"explain (format json, verbose) {sql}").fetchone()
        finally:
            conn.rollback()
        assert row is not None
        plan: dict[str, Any] = row[0][0]["Plan"]
        return PlanInfo(
            total_cost=float(plan["Total Cost"]),
            estimated_rows=int(plan["Plan Rows"]),
            relations=tuple(sorted(_relations(plan))),
            plan=plan,
        )

    def execute(self, s: Session, sql: str, limit: int, timeout_s: int) -> Rows:
        conn = _conn(s)
        started = time.perf_counter()
        try:
            conn.execute(f"set local statement_timeout = {int(timeout_s * 1000)}")
            cur = conn.execute(sql)
            columns = tuple(d.name for d in cur.description or ())
            fetched = cur.fetchmany(limit + 1)
        finally:
            conn.rollback()  # read-only work; never leave a transaction open
        return Rows(
            columns=columns,
            rows=[tuple(r) for r in fetched[:limit]],
            truncated=len(fetched) > limit,
            elapsed_s=round(time.perf_counter() - started, 4),
        )

    def apply_policies(self, policies: PolicySpec) -> None:
        with psycopg.connect(self.admin_conninfo) as conn, conn.transaction():
            for stmt in compile_postgres(policies):
                conn.execute(stmt)

    def freshness(self, node_id: str) -> Freshness:
        if self.graph is None:
            raise ValueError("freshness needs the graph (PostgresAdapter(graph=...))")
        node = self.graph.nodes.get(node_id)
        if not isinstance(node, SourceNode) or not node.loaded_at_field:
            raise ValueError(f"{node_id} is not a source with a loaded_at field")
        with psycopg.connect(self.admin_conninfo) as conn:
            row = conn.execute(
                psycopg.sql.SQL("select max({}) from {}").format(
                    psycopg.sql.Identifier(node.loaded_at_field),
                    psycopg.sql.SQL(node.relation),
                )
            ).fetchone()
        loaded: dt.datetime | None = row[0] if row else None
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
