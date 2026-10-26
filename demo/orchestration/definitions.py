"""Dagster definitions for the Kiwi Retail Co. demo warehouse (PRD §2.1).

- Every dbt model, test and source becomes a Dagster asset (via dagster-dbt).
- `dbt_build_job` runs `dbt build` and is scheduled nightly at 02:00 NZ time.
- `on_dbt_build_failure` is the hook Sentinel will listen on. Phase 11 completes it; for now
  it only logs which run failed.

Run locally: `make demo` (or `uv run dagster dev -f demo/orchestration/definitions.py`).
"""

# No `from __future__ import annotations`: Dagster inspects the context annotations at runtime.
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dagster as dg
from dagster import AssetExecutionContext, RunFailureSensorContext
from dagster_dbt import DbtCliResource, DbtProject, dbt_assets

PROJECT_DIR = Path(__file__).resolve().parents[1] / "kiwi_retail"

kiwi_retail = DbtProject(project_dir=PROJECT_DIR, profiles_dir=PROJECT_DIR)
kiwi_retail.prepare_if_dev()  # parses the project when run under `dagster dev`


@dbt_assets(manifest=kiwi_retail.manifest_path)
def kiwi_retail_dbt_assets(context: AssetExecutionContext, dbt: DbtCliResource) -> Iterator[Any]:
    yield from dbt.cli(["build"], context=context).stream()


dbt_build_job = dg.define_asset_job("dbt_build_job", selection=dg.AssetSelection.all())

nightly_dbt_build = dg.ScheduleDefinition(
    job=dbt_build_job,
    cron_schedule="0 2 * * *",
    execution_timezone="Pacific/Auckland",
)


@dg.run_failure_sensor(monitored_jobs=[dbt_build_job])
def on_dbt_build_failure(context: RunFailureSensorContext) -> None:
    """Stub for Sentinel's event source (PRD §6.3 EventSource). Completed in Phase 11."""
    run = context.dagster_run
    context.log.warning(f"dbt_build_job run {run.run_id} failed: {context.failure_event.message}")


defs = dg.Definitions(
    assets=[kiwi_retail_dbt_assets],
    jobs=[dbt_build_job],
    schedules=[nightly_dbt_build],
    sensors=[on_dbt_build_failure],
    resources={"dbt": DbtCliResource(project_dir=kiwi_retail)},
)
