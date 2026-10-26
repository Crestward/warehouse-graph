"""Unit tests for the synthetic data generator (no database needed)."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from functools import cache

from warehouse_graph.demo.datagen import (
    REFUND_MIGRATION_DATE,
    Table,
    fingerprint,
    generate,
)


@cache
def _tables() -> dict[str, Table]:
    return {t.name: t for t in generate()}


def _col(table: Table, name: str) -> list[object]:
    i = table.column_names.index(name)
    return [row[i] for row in table.rows]


def test_generation_is_deterministic() -> None:
    assert fingerprint(generate(7, customers=300)) == fingerprint(generate(7, customers=300))
    assert fingerprint(generate(7, customers=300)) != fingerprint(generate(8, customers=300))


def test_expected_tables_and_rough_volume() -> None:
    t = _tables()
    assert set(t) == {
        "stores",
        "products",
        "customers",
        "orders",
        "order_items",
        "payments",
        "refunds",
        "employees",
        "campaigns",
        "ad_spend",
        "attributions",
    }
    # small enough for a fast dbt build (Phase 10 re-runs it per injected incident)
    assert sum(len(x.rows) for x in t.values()) < 200_000
    assert len(t["orders"].rows) > 20_000


def test_primary_keys_unique() -> None:
    t = _tables()
    for table, key in [
        ("orders", "order_id"),
        ("order_items", "order_item_id"),
        ("payments", "payment_id"),
        ("customers", "customer_id"),
    ]:
        values = _col(t[table], key)
        assert len(values) == len(set(values)), table


def test_planted_refund_gap_before_migration() -> None:
    refunded = _col(_tables()["refunds"], "refunded_at")
    assert all(isinstance(d, datetime) and d.date() >= REFUND_MIGRATION_DATE for d in refunded)


def test_payments_include_split_tenders_and_failures() -> None:
    payments = _tables()["payments"]
    per_order = Counter(_col(payments, "order_id"))
    assert sum(1 for n in per_order.values() if n > 1) > 1000  # fan-out bait
    assert "failed" in _col(payments, "status")


def test_both_regions_and_currencies_present() -> None:
    orders = _tables()["orders"]
    assert set(_col(orders, "currency_code")) == {"NZD", "AUD"}
    dates = _col(orders, "ordered_at")
    assert min(d.date() for d in dates if isinstance(d, datetime)) >= date(2024, 1, 1)


def test_seasonality_peaks_in_december() -> None:
    by_month = Counter(
        d.month for d in _col(_tables()["orders"], "ordered_at") if isinstance(d, datetime)
    )
    assert by_month[12] > 1.4 * by_month[6]
