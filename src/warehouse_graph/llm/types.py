"""Provider-neutral request/response types for the LLM layer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Effort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class LLMRequest:
    """One model call.

    `system` is the stable prefix and is always marked for prompt caching.
    Keep it byte-identical across calls (no timestamps, IDs or unsorted JSON)
    and put per-call content in `messages`.
    """

    model: str
    messages: list[dict[str, Any]]
    max_tokens: int
    call_site: str  # e.g. "p1.sql_gen"; used for cost reporting, not for caching
    system: str = ""
    effort: Effort | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    cache_messages: bool = False  # auto-cache the growing conversation (agent loops)

    def cache_key(self) -> str:
        """Hash of everything that affects the model's output (not call_site)."""
        payload = {
            "model": self.model,
            "messages": self.messages,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "effort": self.effort,
            "tools": self.tools,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    content: list[dict[str, Any]]  # raw content blocks (text, tool_use, ...)
    stop_reason: str | None
    model: str
    usage: Usage
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMResponse:
        return cls(
            text=data["text"],
            content=data["content"],
            stop_reason=data["stop_reason"],
            model=data["model"],
            usage=Usage(**data["usage"]),
            request_id=data.get("request_id"),
        )
