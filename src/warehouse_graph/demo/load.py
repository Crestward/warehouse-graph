"""Load the synthetic tables into Postgres schema `raw` (local profile).

Every raw table gets a `_loaded_at` column, which dbt source freshness reads. It's set
to the load time, so a fresh load always passes freshness. `stale` back-dates it for the
named tables, which is how the `stale_source` fault (PRD §4.4) is produced on demand.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql

from warehouse_graph.demo.datagen import DEFAULT_SEED, Table, generate

RAW_SCHEMA = "raw"
STALE_BY = timedelta(days=3)


def conninfo_from_env() -> str:
    """libpq connection string for the local profile, from POSTGRES_* variables."""
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "warehouse"),
        password=os.environ.get("POSTGRES_PASSWORD", "local-dev-only"),
        dbname=os.environ.get("POSTGRES_DB", "warehouse"),
        connect_timeout=10,
    )


def load_tables(
    conn: psycopg.Connection,
    tables: Sequence[Table],
    *,
    stale: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, int]:
    """Replace `raw.<table>` for each table. Returns row counts."""
    now = now or datetime.now(UTC)
    stale_set = set(stale)
    unknown = stale_set - {t.name for t in tables}
    if unknown:
        raise ValueError(f"unknown tables for --stale: {sorted(unknown)}")
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(RAW_SCHEMA)))
        for t in tables:
            ident = sql.Identifier(RAW_SCHEMA, t.name)
            cols: list[sql.Composable] = [
                sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(ty)) for c, ty in t.columns
            ]
            cols.append(sql.SQL("_loaded_at timestamptz NOT NULL"))
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident))
            cur.execute(sql.SQL("CREATE TABLE {} ({})").format(ident, sql.SQL(", ").join(cols)))
            loaded_at = now - STALE_BY if t.name in stale_set else now
            copy = sql.SQL("COPY {} ({}) FROM STDIN").format(
                ident, sql.SQL(", ").join(map(sql.Identifier, [*t.column_names, "_loaded_at"]))
            )
            with cur.copy(copy) as cp:
                for row in t.rows:
                    cp.write_row((*row, loaded_at))
            counts[t.name] = len(t.rows)
    conn.commit()
    return counts


def make_stale(
    conn: psycopg.Connection, tables: Iterable[str], *, now: datetime | None = None
) -> None:
    """Back-date `_loaded_at` on existing raw tables (no reload)."""
    now = now or datetime.now(UTC)
    with conn.cursor() as cur:
        for name in tables:
            cur.execute(
                sql.SQL("UPDATE {} SET _loaded_at = %s").format(sql.Identifier(RAW_SCHEMA, name)),
                (now - STALE_BY,),
            )
    conn.commit()


def run(seed: int = DEFAULT_SEED, stale: Sequence[str] = ()) -> dict[str, int]:
    with psycopg.connect(conninfo_from_env()) as conn:
        return load_tables(conn, generate(seed), stale=stale)
