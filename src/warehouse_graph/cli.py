"""Command-line entry point: `warehouse-graph <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warehouse-graph")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("llm-smoke", help="make one cheap model call to verify the LLM wrapper")
    args = parser.parse_args(argv)
    load_dotenv()
    if args.command == "llm-smoke":
        return llm_smoke()
    return 1


if __name__ == "__main__":
    sys.exit(main())
