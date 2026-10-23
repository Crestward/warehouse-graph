from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from warehouse_graph.llm import (
    BatchIncompleteError,
    BudgetExceededError,
    FakeProvider,
    LLMClient,
    LLMRequest,
    LLMResponse,
    Usage,
    cost_usd,
)
from warehouse_graph.llm.anthropic_provider import AnthropicProvider, build_params


def req(text: str = "hi", **kwargs: Any) -> LLMRequest:
    defaults: dict[str, Any] = {
        "model": "claude-sonnet-5-5",
        "max_tokens": 64,
        "call_site": "test",
        "messages": [{"role": "user", "content": text}],
    }
    defaults.update(kwargs)
    return LLMRequest(**defaults)


def ledger_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def client_factory(tmp_path: Path) -> Any:
    def make(provider: FakeProvider, **kwargs: Any) -> LLMClient:
        return LLMClient(
            provider,
            ledger_path=tmp_path / "costs.csv",
            cache_dir=tmp_path / "cache",
            **kwargs,
        )

    return make


# --- pricing -----------------------------------------------------------------


def test_cost_counts_every_token_class() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    # Sonnet 5.5: 2 in + 10 out + 2*1.25 write + 0.20 read
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(14.70)
    assert cost_usd("claude-sonnet-5-5", usage, batch=True) == pytest.approx(7.35)


def test_unknown_model_fails_loudly() -> None:
    with pytest.raises(KeyError):
        cost_usd("not-a-model", Usage())


# --- cache keys ----------------------------------------------------------------


def test_cache_key_ignores_call_site_but_not_content() -> None:
    assert req(call_site="a").cache_key() == req(call_site="b").cache_key()
    assert req("x").cache_key() != req("y").cache_key()
    assert req(effort="low").cache_key() != req(effort="high").cache_key()


# --- client --------------------------------------------------------------------


def test_response_cache_makes_repeat_calls_free(client_factory: Any, tmp_path: Path) -> None:
    provider = FakeProvider()
    client = client_factory(provider)
    first = client.complete(req())
    second = client.complete(req())
    assert first.text == second.text
    assert len(provider.calls) == 1
    rows = ledger_rows(tmp_path / "costs.csv")
    assert [r["response_cache_hit"] for r in rows] == ["0", "1"]
    assert float(rows[1]["cost_usd"]) == 0.0


def test_budget_cap_blocks_calls_once_reached(client_factory: Any) -> None:
    provider = FakeProvider(usage=Usage(input_tokens=1_000_000))  # $2 on Sonnet
    client = client_factory(provider, budget_usd=3.0)
    client.complete(req("a"))
    client.complete(req("b"))  # spent $4 now, over the cap
    with pytest.raises(BudgetExceededError):
        client.complete(req("c"))
    # cached responses are still served for free after the cap
    assert client.complete(req("a")).text == "fake:test"


def test_budget_persists_across_clients(client_factory: Any) -> None:
    provider = FakeProvider(usage=Usage(input_tokens=1_000_000))
    client_factory(provider, budget_usd=1.0).complete(req("a"))
    with pytest.raises(BudgetExceededError):
        client_factory(provider, budget_usd=1.0).complete(req("b"))


def test_complete_many_batches_only_uncached(client_factory: Any, tmp_path: Path) -> None:
    provider = FakeProvider(reply=lambda r: str(r.messages[0]["content"]))
    client = client_factory(provider)
    client.complete(req("a"))
    out = client.complete_many([req("a"), req("b"), req("c")], batch=True)
    assert [r.text for r in out] == ["a", "b", "c"]
    assert len(provider.batch_calls) == 1
    assert [r.messages[0]["content"] for r in provider.batch_calls[0]] == ["b", "c"]
    batch_rows = [r for r in ledger_rows(tmp_path / "costs.csv") if r["batch"] == "1"]
    assert len(batch_rows) == 2


def test_batch_partial_failure_keeps_successes(client_factory: Any) -> None:
    good = LLMResponse("ok", [], "end_turn", "claude-sonnet-5-5", Usage(input_tokens=10))

    class FlakyBatch(FakeProvider):
        def complete_batch(self, requests: Any) -> list[LLMResponse]:
            raise BatchIncompleteError({0: good}, {1: "errored"})

    provider = FlakyBatch()
    client = client_factory(provider)
    with pytest.raises(BatchIncompleteError):
        client.complete_many([req("a"), req("b")])
    # the successful item was cached, so a retry only pays for the failed one
    assert client.complete(req("a")).text == "ok"
    assert provider.calls == []


# --- Anthropic request building (no network) -------------------------------------


def test_build_params_puts_cache_breakpoint_on_system() -> None:
    params = build_params(req(system="stable prefix", effort="low", cache_messages=True))
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["output_config"] == {"effort": "low"}
    assert params["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in params  # removed in API 2026-10-21


def test_build_params_sorts_tools_for_stable_cache_prefix() -> None:
    tools = [{"name": "b"}, {"name": "a"}]
    assert [t["name"] for t in build_params(req(tools=tools))["tools"]] == ["a", "b"]


def test_anthropic_provider_parses_message() -> None:
    message = SimpleNamespace(
        _request_id="req_1",
        to_dict=lambda: {
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "model": "claude-sonnet-5-5",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": 3,
            },
        },
    )
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=lambda **params: message))
    response = AnthropicProvider(client=fake_client).complete(req())  # type: ignore[arg-type]
    assert response.text == "hello"
    assert response.usage == Usage(5, 2, 0, 3)
    assert response.request_id == "req_1"
