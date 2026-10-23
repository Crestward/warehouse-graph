"""The LLMProvider protocol and a deterministic fake for tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from warehouse_graph.llm.types import LLMRequest, LLMResponse, Usage


class LLMProvider(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse:
        """One synchronous call at full price."""
        ...

    def complete_batch(self, requests: Sequence[LLMRequest]) -> list[LLMResponse]:
        """Submit requests as one batch (50% price), block until done.

        Returns responses in the same order as `requests`.
        """
        ...


class FakeProvider:
    """Deterministic provider for tests: no network, no cost surprises."""

    def __init__(
        self,
        reply: Callable[[LLMRequest], str] | None = None,
        usage: Usage | None = None,
    ) -> None:
        self._reply = reply or (lambda req: f"fake:{req.call_site}")
        self._usage = usage or Usage(input_tokens=100, output_tokens=10)
        self.calls: list[LLMRequest] = []
        self.batch_calls: list[list[LLMRequest]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return self._respond(request)

    def complete_batch(self, requests: Sequence[LLMRequest]) -> list[LLMResponse]:
        self.batch_calls.append(list(requests))
        return [self._respond(r) for r in requests]

    def _respond(self, request: LLMRequest) -> LLMResponse:
        text = self._reply(request)
        return LLMResponse(
            text=text,
            content=[{"type": "text", "text": text}],
            stop_reason="end_turn",
            model=request.model,
            usage=self._usage,
        )
