"""Append-only audit log (task 3.9): `audit.events`, written through `AuditWriter`.

The table is created by the policy compiler. Only the `audit_writer` login role may insert;
nobody may update, delete or truncate: a trigger rejects it for every role, including the
table owner (only a superuser could disable the trigger).
Payloads hold ids, SQL, decisions and aggregate stats, never raw result rows (PRD §3.4).
"""

from __future__ import annotations

import json
import os
from typing import Any

import psycopg

from warehouse_graph.governance.postgres import AUDIT_WRITER, role_password


def writer_conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=AUDIT_WRITER,
        password=role_password(AUDIT_WRITER),
        dbname=os.environ.get("POSTGRES_DB", "warehouse"),
        connect_timeout=10,
    )


class AuditWriter:
    def __init__(self, conninfo: str | None = None) -> None:
        self._conn = psycopg.connect(conninfo or writer_conninfo(), autocommit=True)

    def write(
        self,
        principal: str,
        event_type: str,
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> int:
        row = self._conn.execute(
            "insert into audit.events (principal, event_type, request_id, payload)"
            " values (%s, %s, %s, %s::jsonb) returning event_id",
            (principal, event_type, request_id, json.dumps(payload, default=str)),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def close(self) -> None:
        self._conn.close()
