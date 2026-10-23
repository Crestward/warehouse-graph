"""Anthropic implementation of LLMProvider (anthropic SDK 2.x, API 2026-10-21)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import anthropic

from warehouse_graph.llm.types import LLMRequest, LLMResponse, Usage


class BatchIncompleteError(RuntimeError):
    """Some batch items failed. Successful items are kept so they aren't paid for twice."""

    def __init__(self, succeeded: dict[int, LLMResponse], failed: dict[int, str]) -> None:
        super().__init__(f"{len(failed)} of {len(succeeded) + len(failed)} batch items failed")
        self.succeeded = succeeded
        self.failed = failed


def build_params(request: LLMRequest) -> dict[str, Any]:
    """Translate an LLMRequest into Messages API params.

    Order matters for prompt caching (tools -> system -> messages): the system
    prompt carries an explicit breakpoint so the shared prefix is always a read
    point, and `cache_messages` adds automatic caching for the growing tail.
    """
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": request.messages,
    }
    if request.system:
        params["system"] = [
            {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
        ]
    if request.tools:
        params["tools"] = sorted(request.tools, key=lambda t: str(t.get("name", "")))
    if request.effort is not None:
        params["output_config"] = {"effort": request.effort}
    if request.cache_messages:
        params["cache_control"] = {"type": "ephemeral"}
    return params


def to_response(message: Any) -> LLMResponse:
    data = message.to_dict()
    content: list[dict[str, Any]] = data.get("content", [])
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
    usage = data.get("usage") or {}
    return LLMResponse(
        text=text,
        content=content,
        stop_reason=data.get("stop_reason"),
        model=data.get("model", ""),
        usage=Usage(
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens") or 0,
            cache_read_input_tokens=usage.get("cache_read_input_tokens") or 0,
        ),
        request_id=getattr(message, "_request_id", None),
    )


class AnthropicProvider:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        max_retries: int = 4,
        poll_interval_s: float = 30.0,
    ) -> None:
        # Credentials come from the environment (ANTHROPIC_API_KEY); never hardcode.
        # The SDK retries 408/409/429/5xx and connection errors with backoff.
        self._client = client or anthropic.Anthropic(max_retries=max_retries)
        self._poll_interval_s = poll_interval_s

    def complete(self, request: LLMRequest) -> LLMResponse:
        message = self._client.messages.create(**build_params(request))
        return to_response(message)

    def complete_batch(self, requests: Sequence[LLMRequest]) -> list[LLMResponse]:
        if not requests:
            return []
        # Params are built as plain dicts (same shape as messages.create), so the
        # SDK's per-field TypedDicts can't be checked statically here.
        batch_requests: list[Any] = [
            {"custom_id": f"r{i}", "params": build_params(req)} for i, req in enumerate(requests)
        ]
        batch = self._client.messages.batches.create(requests=batch_requests)
        while batch.processing_status != "ended":
            time.sleep(self._poll_interval_s)
            batch = self._client.messages.batches.retrieve(batch.id)

        succeeded: dict[int, LLMResponse] = {}
        failed: dict[int, str] = {}
        for item in self._client.messages.batches.results(batch.id):
            index = int(item.custom_id[1:])
            if item.result.type == "succeeded":
                succeeded[index] = to_response(item.result.message)
            else:
                failed[index] = item.result.type
        if failed or len(succeeded) != len(requests):
            missing = {i: "missing" for i in range(len(requests)) if i not in succeeded}
            raise BatchIncompleteError(succeeded, {**missing, **failed})
        return [succeeded[i] for i in range(len(requests))]
