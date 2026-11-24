"""Adapter interfaces (PRD §6.3) and the local-profile implementations."""

from warehouse_graph.adapters.postgres import PostgresAdapter
from warehouse_graph.adapters.protocols import (
    EventSource,
    Freshness,
    LLMProvider,
    LogSource,
    MetadataSource,
    Notification,
    Notifier,
    PipelineEvent,
    PlanInfo,
    ResultStats,
    Rows,
    Session,
    WarehouseAdapter,
)

__all__ = [
    "EventSource",
    "Freshness",
    "LLMProvider",
    "LogSource",
    "MetadataSource",
    "Notification",
    "Notifier",
    "PipelineEvent",
    "PlanInfo",
    "PostgresAdapter",
    "ResultStats",
    "Rows",
    "Session",
    "WarehouseAdapter",
]
