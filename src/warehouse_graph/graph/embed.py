"""Text embeddings for `search_nodes`: one local sentence-transformer on CPU (PRD §2.2).

No embedding API, so no per-call cost and no second vendor. Unit tests use `HashEmbedder`,
which needs no model download.
"""

from __future__ import annotations

import hashlib
import re
from functools import cache
from typing import Protocol

import numpy as np
import numpy.typing as npt

from warehouse_graph.graph.schema import (
    CaveatNode,
    ColumnNode,
    ExposureNode,
    MetricNode,
    ModelNode,
    Node,
    QueryExampleNode,
)

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HASH_MODEL = "hash"

Matrix = npt.NDArray[np.float32]


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> Matrix:
        """Unit-length row vectors, one per text."""
        ...


class SentenceTransformerEmbedder:
    def __init__(self, name: str = DEFAULT_MODEL) -> None:
        self.name = name

    def embed(self, texts: list[str]) -> Matrix:
        vectors = _load_model(self.name).encode(
            texts, batch_size=64, normalize_embeddings=True, convert_to_numpy=True
        )
        return np.asarray(vectors, dtype=np.float32)


@cache
def _load_model(name: str):  # type: ignore[no-untyped-def]  # torch types stay out of mypy
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name, device="cpu")


class HashEmbedder:
    """Deterministic bag-of-words hashing. For tests only: no semantics, no download."""

    name = HASH_MODEL

    def __init__(self, dims: int = 256) -> None:
        self.dims = dims

    def embed(self, texts: list[str]) -> Matrix:
        out = np.zeros((len(texts), self.dims), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in tokenize(text):
                h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest())
                out[i, h % self.dims] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return (out / np.where(norms == 0, 1, norms)).astype(np.float32)


def get_embedder(name: str) -> Embedder:
    return HashEmbedder() if name == HASH_MODEL else SentenceTransformerEmbedder(name)


_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens; snake_case and dotted ids split into words."""
    return _TOKEN.findall(text.lower())


def node_text(node: Node, columns: list[str] | None = None) -> str:
    """What gets embedded and BM25-indexed for a node."""
    parts = [node.type.value.replace("_", " "), node.name.replace("_", " ")]
    if isinstance(node, MetricNode | ExposureNode) and node.label:
        parts.append(node.label)
    if isinstance(node, ColumnNode):
        parts.append(f"in {node.parent.split('.', 1)[1].replace('_', ' ')}")
    parts.append(node.description)
    if isinstance(node, QueryExampleNode) and node.notes:
        parts.append(node.notes)
    if isinstance(node, CaveatNode | QueryExampleNode | MetricNode | ModelNode) and node.tags:
        parts.append(" ".join(node.tags))
    if columns:
        parts.append("columns: " + ", ".join(columns))
    return ". ".join(p for p in parts if p)
