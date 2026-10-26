"""Command-line entry point: `warehouse-graph <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from warehouse_graph.demo.datagen import DEFAULT_SEED
from warehouse_graph.llm import AnthropicProvider, LLMClient, LLMRequest

SMOKE_MODEL = "claude-haiku-4-5"


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def llm_smoke() -> int:
    """One tiny real call through the wrapper; logs a row to costs.csv."""
    client = LLMClient(AnthropicProvider(), cache_dir=None, run_id="smoke")
    response = client.complete(
        LLMRequest(
            model=SMOKE_MODEL,
            max_tokens=16,
            call_site="phase0.smoke",
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
    )
    print(f"model={response.model} reply={response.text!r} spent_total=${client.spent_usd:.6f}")
    return 0


def demo_data(seed: int, stale: list[str]) -> int:
    """Generate the Kiwi Retail Co. raw tables and load them into local Postgres."""
    from warehouse_graph.demo.load import run

    counts = run(seed, stale)
    total = sum(counts.values())
    print(f"loaded {total} rows into raw.* (seed={seed}, stale={stale or 'none'})")
    for name, n in counts.items():
        print(f"  raw.{name:<13} {n:>7}")
    return 0


def demo_build() -> int:
    """Build the demo dbt project and copy its four artifacts to demo/artifacts/."""
    from warehouse_graph.demo.build import ARTIFACTS, ARTIFACTS_DIR, run

    results = run()
    for step in results:
        print(f"{'ok  ' if step.returncode == 0 else 'FAIL'} {step.command}")
    missing = [a for a in ARTIFACTS if not (ARTIFACTS_DIR / a).exists()]
    print(f"artifacts in {ARTIFACTS_DIR}: " + ("all 4" if not missing else f"missing {missing}"))
    return 0 if results[0].returncode == 0 and results[-1].returncode == 0 and not missing else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warehouse-graph")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("llm-smoke", help="make one cheap model call to verify the LLM wrapper")
    demo = sub.add_parser("demo-data", help="generate and load the synthetic raw tables")
    demo.add_argument("--seed", type=int, default=DEFAULT_SEED)
    demo.add_argument(
        "--stale", nargs="*", default=[], metavar="TABLE", help="back-date _loaded_at"
    )
    sub.add_parser("demo-build", help="dbt build + freshness + docs; collect artifacts")
    args = parser.parse_args(argv)
    load_dotenv()
    if args.command == "llm-smoke":
        return llm_smoke()
    if args.command == "demo-data":
        return demo_data(args.seed, args.stale)
    if args.command == "demo-build":
        return demo_build()
    return 1


if __name__ == "__main__":
    sys.exit(main())
