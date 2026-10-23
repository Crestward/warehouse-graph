"""LLMClient: the single entry point for every model call in the project.

Adds the cost levers from PRD §5.2 on top of any LLMProvider:
- a response cache (identical request -> stored response, $0)
- a cost ledger (`costs.csv`) with cache-read tokens, batch flag and cache hits
- a hard budget cap across all runs (default US$150)
- batch rounds via `complete_many(..., batch=True)`
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from warehouse_graph.llm.anthropic_provider import BatchIncompleteError
from warehouse_graph.llm.pricing import cost_usd
from warehouse_graph.llm.provider import LLMProvider
from warehouse_graph.llm.types import LLMRequest, LLMResponse

DEFAULT_BUDGET_USD = 150.0

LEDGER_FIELDS = [
    "timestamp",
    "run_id",
    "call_site",
    "model",
    "batch",
    "response_cache_hit",
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "cost_usd",
    "request_id",
]


class BudgetExceededError(RuntimeError):
    pass


class ResponseCache:
    """Stores responses as JSON files keyed by the request hash."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.json"

    def get(self, request: LLMRequest) -> LLMResponse | None:
        path = self._path(request.cache_key())
        if not path.exists():
            return None
        return LLMResponse.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def put(self, request: LLMRequest, response: LLMResponse) -> None:
        path = self._path(request.cache_key())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(response.to_dict(), sort_keys=True), encoding="utf-8")


class CostLedger:
    """Append-only CSV of every call, including response-cache hits at $0."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.total_usd = self._load_total()

    def _load_total(self) -> float:
        if not self._path.exists():
            return 0.0
        with self._path.open(newline="", encoding="utf-8") as f:
            return sum(float(row["cost_usd"]) for row in csv.DictReader(f))

    def record(
        self,
        *,
        run_id: str,
        request: LLMRequest,
        response: LLMResponse,
        batch: bool,
        cache_hit: bool,
    ) -> float:
        cost = 0.0 if cache_hit else cost_usd(request.model, response.usage, batch=batch)
        new_file = not self._path.exists()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "call_site": request.call_site,
                    "model": request.model,
                    "batch": int(batch),
                    "response_cache_hit": int(cache_hit),
                    "input_tokens": response.usage.input_tokens,
                    "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
                    "cache_read_input_tokens": response.usage.cache_read_input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cost_usd": f"{cost:.6f}",
                    "request_id": response.request_id or "",
                }
            )
        self.total_usd += cost
        return cost


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        ledger_path: Path = Path("costs.csv"),
        cache_dir: Path | None = Path(".llm-cache"),
        budget_usd: float | None = None,
        run_id: str = "adhoc",
    ) -> None:
        self._provider = provider
        self._ledger = CostLedger(ledger_path)
        self._cache = ResponseCache(cache_dir) if cache_dir is not None else None
        self._budget_usd = (
            budget_usd
            if budget_usd is not None
            else float(os.environ.get("LLM_BUDGET_USD", DEFAULT_BUDGET_USD))
        )
        self.run_id = run_id

    @property
    def spent_usd(self) -> float:
        return self._ledger.total_usd

    def _check_budget(self) -> None:
        if self._ledger.total_usd >= self._budget_usd:
            raise BudgetExceededError(
                f"LLM spend ${self._ledger.total_usd:.2f} has reached the "
                f"${self._budget_usd:.2f} cap (PRD §5.3). Raise LLM_BUDGET_USD deliberately."
            )

    def _cached(self, request: LLMRequest) -> LLMResponse | None:
        if self._cache is None:
            return None
        hit = self._cache.get(request)
        if hit is not None:
            self._ledger.record(
                run_id=self.run_id, request=request, response=hit, batch=False, cache_hit=True
            )
        return hit

    def _store(self, request: LLMRequest, response: LLMResponse, *, batch: bool) -> None:
        self._ledger.record(
            run_id=self.run_id, request=request, response=response, batch=batch, cache_hit=False
        )
        if self._cache is not None:
            self._cache.put(request, response)

    def complete(self, request: LLMRequest) -> LLMResponse:
        hit = self._cached(request)
        if hit is not None:
            return hit
        self._check_budget()
        response = self._provider.complete(request)
        self._store(request, response, batch=False)
        return response

    def complete_many(
        self, requests: Sequence[LLMRequest], *, batch: bool = True
    ) -> list[LLMResponse]:
        """Answer many independent requests; one call of this is one batch round.

        Cached requests are served for free; the rest go to the Batch API
        (50% off) when `batch` is true, or sequentially otherwise.
        """
        results: list[LLMResponse | None] = [self._cached(r) for r in requests]
        pending = [i for i, r in enumerate(results) if r is None]
        if pending:
            self._check_budget()
            todo = [requests[i] for i in pending]
            if batch:
                try:
                    responses = self._provider.complete_batch(todo)
                except BatchIncompleteError as exc:
                    for j, response in exc.succeeded.items():
                        self._store(todo[j], response, batch=True)
                    raise
            else:
                responses = [self._provider.complete(r) for r in todo]
            for i, response in zip(pending, responses, strict=True):
                self._store(requests[i], response, batch=batch)
                results[i] = response
        return [r for r in results if r is not None]
