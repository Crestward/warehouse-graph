"""Adapter interfaces (PRD §6.3) and the local-profile implementations."""

from warehouse_graph.adapters.postgres import PostgresAdapter
from warehouse_graph.adapters.protocols import (
    AccessDenied,
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
    UnknownObject,
    WarehouseAdapter,
    WarehouseError,
)

__all__ = [
    "AccessDenied",
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
    "UnknownObject",
    "WarehouseAdapter",
    "WarehouseError",
]
