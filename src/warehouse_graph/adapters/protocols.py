"""Adapter interfaces shared by both projects (PRD §6.3, task 3.1).

Every implementation that will ever exist is listed in the PRD (§1.4.5 rule 4):

    WarehouseAdapter   Postgres (here, Phase 3) | Databricks (Phase 9)
    MetadataSource     dbt artifacts (warehouse_graph.graph.dbt, Phase 2)
    EventSource        Dagster run-failure sensor (Phase 11) | Databricks webhook queue (Phase 16)
    LogSource          local files (Phase 11) | Databricks Jobs run output (Phase 16)
    LLMProvider        Anthropic + a fake (warehouse_graph.llm, Phase 0)
    Notifier           console (Phase 14) | Microsoft Teams (Phase 16)
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from warehouse_graph.governance.policies import PolicySpec, Principal
from warehouse_graph.graph.dbt import MetadataSource
from warehouse_graph.llm.provider import LLMProvider

__all__ = [
    "ColumnStats",
    "EventSource",
    "Freshness",
    "LLMProvider",
    "LogSource",
    "MetadataSource",
    "Notification",
    "Notifier",
    "PipelineEvent",
    "PlanInfo",
    "ResultStats",
    "Rows",
    "Session",
    "WarehouseAdapter",
]


class Session(Protocol):
    """A connection that runs as exactly one principal."""

    principal: Principal

    def close(self) -> None: ...


@dataclass(frozen=True)
class PlanInfo:
    total_cost: float
    estimated_rows: int
    relations: tuple[str, ...]  # schema.table read by the plan
    plan: dict[str, Any]


@dataclass(frozen=True)
class ColumnStats:
    name: str
    null_rate: float
    distinct: int
    min: Any = None
    max: Any = None


@dataclass(frozen=True)
class ResultStats:
    """What an LLM may see about a result: shape and aggregates, never rows (PRD §3.4)."""

    row_count: int
    truncated: bool
    columns: tuple[ColumnStats, ...]


@dataclass(frozen=True)
class Rows:
    """A query result. Rows go to the user from code; only `stats()` may reach an LLM."""

    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]
    truncated: bool
    elapsed_s: float

    def stats(self) -> ResultStats:
        out: list[ColumnStats] = []
        for i, name in enumerate(self.columns):
            values = [r[i] for r in self.rows]
            present = [v for v in values if v is not None]
            try:
                lo, hi = (min(present), max(present)) if present else (None, None)
            except TypeError:  # mixed or unorderable types
                lo = hi = None
            if isinstance(lo, float) and math.isnan(lo):
                lo = hi = None
            out.append(
                ColumnStats(
                    name=name,
                    null_rate=round(1 - len(present) / len(values), 4) if values else 0.0,
                    distinct=len({repr(v) for v in present}),
                    min=lo,
                    max=hi,
                )
            )
        return ResultStats(row_count=len(self.rows), truncated=self.truncated, columns=tuple(out))


@dataclass(frozen=True)
class Freshness:
    node_id: str
    max_loaded_at: dt.datetime | None
    age_hours: float | None
    warn_after_hours: float | None
    error_after_hours: float | None
    status: Literal["pass", "warn", "error", "unknown"]


class WarehouseAdapter(Protocol):
    def session_as(self, principal: Principal) -> Session: ...

    def explain(self, s: Session, sql: str) -> PlanInfo: ...

    def execute(self, s: Session, sql: str, limit: int, timeout_s: int) -> Rows: ...

    def apply_policies(self, policies: PolicySpec) -> None: ...

    def freshness(self, node_id: str) -> Freshness: ...


@dataclass(frozen=True)
class PipelineEvent:
    """Something went wrong in a pipeline run: Sentinel's trigger (PRD §4.3)."""

    event_id: str
    kind: str  # run_failure | test_failure | freshness_failure | manual
    occurred_at: dt.datetime
    run_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class EventSource(Protocol):
    def events(self) -> Iterator[PipelineEvent]: ...


class LogSource(Protocol):
    def fetch(self, run_id: str) -> str:
        """The raw log text of one pipeline run."""
        ...


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    severity: Literal["info", "warning", "critical"]
    link: str | None = None


class Notifier(Protocol):
    def send(self, notification: Notification) -> None: ...
