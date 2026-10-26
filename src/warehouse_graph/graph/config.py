"""`warehouse-graph.toml`: where the graph's inputs are and which embedding model to use."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from warehouse_graph.graph.embed import DEFAULT_MODEL

CONFIG_FILE = Path("warehouse-graph.toml")


@dataclass(frozen=True)
class GraphConfig:
    artifacts_dir: Path = Path("demo/artifacts")
    caveats: Path | None = Path("demo/caveats.yml")
    query_examples: Path | None = Path("demo/query_examples.yml")
    output: Path = Path("build/graph.wgz")
    embedding_model: str = DEFAULT_MODEL

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> GraphConfig:
        if not path.exists():
            return cls()
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("graph", {})
        base = path.parent
        paths = {
            k: base / raw[k]
            for k in ("artifacts_dir", "caveats", "query_examples", "output")
            if k in raw
        }
        return cls(**paths, embedding_model=raw.get("embedding_model", DEFAULT_MODEL))
