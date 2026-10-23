"""Per-model token prices and cost calculation.

Prices are USD per 1M tokens (Claude API list prices, checked Oct 2026).
Re-check before publishing results; see PRD §5.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from warehouse_graph.llm.types import Usage

CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL write premium
BATCH_DISCOUNT = 0.5


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(input=1.0, output=5.0, cache_read=0.10),
    "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0, cache_read=0.20),
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_read=0.20),
}


class UnknownModelError(KeyError):
    pass


def cost_usd(model: str, usage: Usage, *, batch: bool = False) -> float:
    """Return the USD cost of one call.

    Uses the conservative cache-read price where sources disagree, so the
    budget cap errs on the side of stopping early.
    """
    try:
        price = PRICES[model]
    except KeyError as exc:
        raise UnknownModelError(f"No price for model {model!r}; add it to PRICES") from exc
    total = (
        usage.input_tokens * price.input
        + usage.cache_creation_input_tokens * price.input * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_input_tokens * price.cache_read
        + usage.output_tokens * price.output
    ) / 1_000_000
    return total * BATCH_DISCOUNT if batch else total
