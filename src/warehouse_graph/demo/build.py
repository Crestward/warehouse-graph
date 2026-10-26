"""Build the demo dbt project and collect the four artifacts the graph is built from.

dbt overwrites `target/run_results.json` on every command, so each artifact is copied to
`demo/artifacts/` right after the command that produces it:

    dbt build            -> run_results.json
    dbt source freshness -> sources.json
    dbt docs generate    -> catalog.json, manifest.json
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path("demo/kiwi_retail")
ARTIFACTS_DIR = Path("demo/artifacts")
ARTIFACTS = ("manifest.json", "catalog.json", "run_results.json", "sources.json")


@dataclass(frozen=True)
class StepResult:
    command: str
    returncode: int


def _dbt(args: list[str], project_dir: Path) -> int:
    exe = shutil.which("dbt")
    if exe is None:
        raise RuntimeError("dbt not found; run `uv sync` (the demo dependency group)")
    cmd = [exe, *args, "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
    return subprocess.run(cmd, check=False).returncode


def _collect(project_dir: Path, artifacts_dir: Path, *names: str) -> None:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(project_dir / "target" / name, artifacts_dir / name)


def run(project_dir: Path = PROJECT_DIR, artifacts_dir: Path = ARTIFACTS_DIR) -> list[StepResult]:
    """Run build, freshness and docs; stop early only if the build itself fails."""
    results: list[StepResult] = []

    rc = _dbt(["build"], project_dir)
    results.append(StepResult("dbt build", rc))
    if rc != 0:
        return results
    _collect(project_dir, artifacts_dir, "run_results.json")

    # Freshness failures are reported, not fatal: a stale source is a valid (injected) state.
    rc = _dbt(["source", "freshness"], project_dir)
    results.append(StepResult("dbt source freshness", rc))
    if (project_dir / "target" / "sources.json").exists():
        _collect(project_dir, artifacts_dir, "sources.json")

    rc = _dbt(["docs", "generate"], project_dir)
    results.append(StepResult("dbt docs generate", rc))
    if rc == 0:
        _collect(project_dir, artifacts_dir, "catalog.json", "manifest.json")
    return results
