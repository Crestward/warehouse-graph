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


def graph_build(config_path: Path, embedding_model: str | None, stats: Path | None) -> int:
    """dbt artifacts + caveats + query examples -> the graph file."""
    import dataclasses
    import time

    from warehouse_graph.graph import DbtArtifacts, GraphConfig, build
    from warehouse_graph.graph.embed import get_embedder
    from warehouse_graph.graph.render import stats_markdown

    config = GraphConfig.load(config_path)
    if embedding_model:
        config = dataclasses.replace(config, embedding_model=embedding_model)
    started = time.perf_counter()
    artifact = build(
        DbtArtifacts(config.artifacts_dir),
        get_embedder(config.embedding_model),
        caveats=config.caveats,
        query_examples=config.query_examples,
    )
    artifact.save(config.output)
    s = artifact.meta.stats
    print(
        f"wrote {config.output}: {sum(s.nodes_by_type.values())} nodes,"
        f" {sum(s.edges_by_type.values())} edges, column lineage"
        f" {s.column_lineage_coverage:.1%}, {time.perf_counter() - started:.1f} s"
    )
    if stats:
        stats.write_text(stats_markdown(artifact.meta), encoding="utf-8")
        print(f"wrote {stats}")
    return 0


def graph_lineage(
    node_id: str, direction: str, depth: int, level: str, graph_path: Path | None
) -> int:
    """Print a node's lineage as a Mermaid flowchart (README diagrams)."""
    from warehouse_graph.graph import GraphArtifact, GraphConfig, GraphQuery
    from warehouse_graph.graph.embed import HashEmbedder
    from warehouse_graph.graph.render import mermaid

    path = graph_path or GraphConfig.load().output
    query = GraphQuery(GraphArtifact.load(path), embedder=HashEmbedder())  # no search here
    print(mermaid(query.get_lineage(node_id, direction, depth, level)))  # type: ignore[arg-type]
    return 0


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
    gb = sub.add_parser("build", help="build the graph file from the dbt artifacts")
    gb.add_argument("--config", type=Path, default=Path("warehouse-graph.toml"))
    gb.add_argument("--embedding-model", help="override the config (`hash` = no model, tests)")
    gb.add_argument("--stats", type=Path, metavar="MD", help="also write graph stats markdown")
    gl = sub.add_parser("lineage", help="print a node's lineage as a Mermaid diagram")
    gl.add_argument("node_id")
    gl.add_argument("--direction", choices=["upstream", "downstream", "both"], default="upstream")
    gl.add_argument("--depth", type=int, default=10)
    gl.add_argument("--level", choices=["model", "column"], default="model")
    gl.add_argument("--graph", type=Path, help="graph file (default: the config's output)")
    args = parser.parse_args(argv)
    load_dotenv()
    if args.command == "llm-smoke":
        return llm_smoke()
    if args.command == "demo-data":
        return demo_data(args.seed, args.stale)
    if args.command == "demo-build":
        return demo_build()
    if args.command == "build":
        return graph_build(args.config, args.embedding_model, args.stats)
    if args.command == "lineage":
        return graph_lineage(args.node_id, args.direction, args.depth, args.level, args.graph)
    return 1


if __name__ == "__main__":
    sys.exit(main())
