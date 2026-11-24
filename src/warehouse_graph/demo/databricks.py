"""The demo warehouse on Unity Catalog, from the laptop (task 9.2): load `raw`, run dbt.

raw     CSV files to a Unity Catalog volume (Files API), then CREATE TABLE AS read_files.
dbt     the same project, target `databricks`. dbt-databricks runs isolated through uvx, and
        into its own target directory, so it can't disturb the local artifacts that the graph
        is built from.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from warehouse_graph.adapters.databricks import catalog_from_env, connect
from warehouse_graph.demo.build import PROJECT_DIR
from warehouse_graph.demo.datagen import DEFAULT_SEED, Table, generate
from warehouse_graph.demo.load import RAW_SCHEMA, STALE_BY
from warehouse_graph.governance.unity import ident

TARGET_DIR = "target_databricks"
DBT = ("--from", "dbt-core>=1.10", "--with", "dbt-databricks>=1.10")
NULL = "~null~"  # no backslash: it would be unescaped twice on the way
_TYPES = {
    "text": "string",
    "integer": "int",
    "bigint": "bigint",
    "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamp_ntz",
}


def databricks_type(postgres: str) -> str:
    base, _, rest = postgres.lower().partition("(")
    words = base.split()
    if words[0] == "numeric":
        return f"decimal({rest}" if rest else "decimal(38, 6)"
    if words[0] not in _TYPES:
        raise ValueError(f"no Databricks type for {postgres!r}")
    return _TYPES[words[0]]


def to_csv(table: Table, loaded_at: datetime) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([*table.column_names, "_loaded_at"])
    stamp = loaded_at.isoformat()
    for row in table.rows:
        writer.writerow([NULL if v is None else v for v in (*row, stamp)])
    return out.getvalue().encode("utf-8")


def _upload(path: str, body: bytes) -> None:
    host = os.environ["DATABRICKS_HOST"].rstrip("/")
    request = urllib.request.Request(
        f"{host}/api/2.0/fs/files{path}?overwrite=true",
        method="PUT",
        data=body,
        headers={"Authorization": f"Bearer {os.environ['DATABRICKS_TOKEN']}"},
    )
    urllib.request.urlopen(request, timeout=120).close()


def load(
    seed: int = DEFAULT_SEED, stale: Sequence[str] = (), now: datetime | None = None
) -> dict[str, int]:
    """Replace every raw table. Returns row counts, as counted in Unity Catalog."""
    now = now or datetime.now(UTC)
    catalog = catalog_from_env()
    tables = generate(seed)
    unknown = set(stale) - {t.name for t in tables}
    if unknown:
        raise ValueError(f"unknown tables for --stale: {sorted(unknown)}")
    raw = f"{ident(catalog)}.{ident(RAW_SCHEMA)}"
    counts: dict[str, int] = {}
    with connect(os.environ["DATABRICKS_TOKEN"]) as conn, conn.cursor() as cur:
        cur.execute(f"create catalog if not exists {ident(catalog)}")
        cur.execute(f"create schema if not exists {raw}")
        cur.execute(f"create volume if not exists {raw}.`load`")
        for t in tables:
            path = f"/Volumes/{catalog}/{RAW_SCHEMA}/load/{t.name}.csv"
            _upload(path, to_csv(t, now - STALE_BY if t.name in stale else now))
            columns = [(c, databricks_type(ty)) for c, ty in t.columns]
            columns.append(("_loaded_at", "timestamp"))
            schema = ", ".join(f"{ident(c)} {ty}" for c, ty in columns)
            names = ", ".join(ident(c) for c, _ in columns)
            cur.execute(
                f"create or replace table {raw}.{ident(t.name)} as select {names}"
                f" from read_files('{path}', format => 'csv', header => true, multiLine => true,"
                f" nullValue => '{NULL}', escape => '\"', mode => 'FAILFAST',"
                f" schema => '{schema}')"
            )
            cur.execute(f"select count(*) from {raw}.{ident(t.name)}")
            counts[t.name] = int(cur.fetchone()[0])
            if counts[t.name] != len(t.rows):
                raise RuntimeError(f"raw.{t.name}: loaded {counts[t.name]} of {len(t.rows)} rows")
    return counts


def dbt(args: Sequence[str], project_dir: Path = PROJECT_DIR) -> int:
    uvx = shutil.which("uvx")
    if uvx is None:
        raise RuntimeError("uvx not found; install uv")
    env = {
        **os.environ,
        "DATABRICKS_CATALOG": catalog_from_env(),
        "DBT_DATABRICKS_HOST": os.environ["DATABRICKS_HOST"].removeprefix("https://").split("/")[0],
        "MSYS_NO_PATHCONV": "1",  # Git Bash would rewrite the HTTP path
    }
    command = [uvx, "--python", "3.12", *DBT, "dbt", *args]
    command += ["--target", "databricks", "--target-path", TARGET_DIR]
    command += ["--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
    return subprocess.run(command, check=False, env=env).returncode
