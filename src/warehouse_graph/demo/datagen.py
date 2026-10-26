"""Deterministic synthetic data for "Kiwi Retail Co." (the demo warehouse).

`generate(seed)` is pure: the same seed always yields the same rows, so eval answers
written against this data stay valid. Load-time columns (`_loaded_at`) are added by
`load.py`, not here, because source freshness has to be relative to "now".

Realism and planted messiness (see docs/planted-traps.md):
- two regions (NZ, AU) with local currencies (NZD, AUD); amounts are in store currency
- seasonality: Black Friday / Christmas peak, January sales, winter dip; ~15%/yr growth
- split payments and failed payment attempts (naive joins to payments fan out)
- refunds before 2024-03-01 were never migrated from the old POS (net revenue overstated)
- PII: customer email/phone, employee salary
"""

from __future__ import annotations

import bisect
import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

START = date(2024, 1, 1)
END = date(2026, 9, 30)
REFUND_MIGRATION_DATE = date(2024, 3, 1)
DEFAULT_SEED = 42

Row = tuple[object, ...]


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[tuple[str, str], ...]  # (name, postgres type)
    rows: list[Row]

    @property
    def column_names(self) -> list[str]:
        return [c for c, _ in self.columns]


STORES: list[tuple[int, str, str, str, str, date]] = [
    # store_id, name, city, region_code, channel, opened_on
    (1, "Auckland Queen St", "Auckland", "NZ", "store", date(2015, 3, 1)),
    (2, "Auckland Sylvia Park", "Auckland", "NZ", "store", date(2018, 9, 15)),
    (3, "Wellington Lambton Quay", "Wellington", "NZ", "store", date(2016, 6, 1)),
    (4, "Christchurch Riccarton", "Christchurch", "NZ", "store", date(2017, 11, 1)),
    (5, "Hamilton The Base", "Hamilton", "NZ", "store", date(2019, 4, 1)),
    (6, "Dunedin George St", "Dunedin", "NZ", "store", date(2020, 2, 1)),
    (7, "Online NZ", "Auckland", "NZ", "online", date(2015, 1, 1)),
    (8, "Sydney Pitt St", "Sydney", "AU", "store", date(2021, 8, 1)),
    (9, "Melbourne Bourke St", "Melbourne", "AU", "store", date(2022, 3, 1)),
    (10, "Brisbane Queen St", "Brisbane", "AU", "store", date(2023, 5, 1)),
    (11, "Perth Hay St", "Perth", "AU", "store", date(2024, 7, 1)),
    (12, "Online AU", "Sydney", "AU", "online", date(2021, 8, 1)),
]
CURRENCY = {"NZ": "NZD", "AU": "AUD"}
AUD_PRICE_FACTOR = 0.92  # AU shelf prices in AUD relative to NZD list prices

NZ_CITIES = ["Auckland", "Wellington", "Christchurch", "Hamilton", "Dunedin", "Tauranga"]
AU_CITIES = ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide"]
FIRST_NAMES = [
    "Aroha",
    "Mia",
    "Olivia",
    "Isla",
    "Charlotte",
    "Amelia",
    "Ruby",
    "Hana",
    "Grace",
    "Zoe",
    "Liam",
    "Noah",
    "Oliver",
    "Jack",
    "Leo",
    "Nikau",
    "Mason",
    "Wiremu",
    "Lucas",
    "Arjun",
    "Priya",
    "Mei",
    "Sione",
    "Ana",
    "Tama",
    "Ethan",
    "Chloe",
    "Harper",
    "Kai",
    "Sofia",
]
LAST_NAMES = [
    "Smith",
    "Williams",
    "Brown",
    "Wilson",
    "Taylor",
    "Ngata",
    "Te Whata",
    "Patel",
    "Singh",
    "Nguyen",
    "Chen",
    "Wang",
    "Kumar",
    "Tupou",
    "Fifita",
    "Jones",
    "Walker",
    "Clark",
    "Young",
    "Martin",
    "King",
    "Thompson",
    "Harris",
    "Lee",
    "Anderson",
]
CATEGORIES = {
    # category: (count, min price, max price, cost ratio)
    "Apparel": (40, 19.0, 149.0, 0.45),
    "Footwear": (20, 59.0, 249.0, 0.50),
    "Outdoor": (25, 29.0, 499.0, 0.55),
    "Home": (20, 9.0, 189.0, 0.40),
    "Accessories": (15, 9.0, 99.0, 0.35),
}
PRODUCT_WORDS = {
    "Apparel": ["Merino Crew", "Puffer Jacket", "Rain Shell", "Linen Shirt", "Hoodie", "Chinos"],
    "Footwear": ["Trail Runner", "Jandal", "Leather Boot", "Court Sneaker", "Hiking Boot"],
    "Outdoor": ["Day Pack", "Tent 2P", "Sleeping Bag", "Trekking Poles", "Headlamp", "Dry Bag"],
    "Home": ["Wool Throw", "Ceramic Mug", "Candle", "Cushion", "Flax Basket"],
    "Accessories": ["Beanie", "Sunglasses", "Drink Bottle", "Cap", "Scarf"],
}
SEASONALITY = {
    1: 1.10,
    2: 0.90,
    3: 0.95,
    4: 0.95,
    5: 0.90,
    6: 0.85,
    7: 1.00,
    8: 0.90,
    9: 0.95,
    10: 1.00,
    11: 1.40,
    12: 1.80,
}
EMPLOYEE_ROLES = [
    ("Store Manager", 1, 78000, 98000),
    ("Assistant Manager", 1, 62000, 74000),
    ("Sales Associate", 7, 47000, 56000),
    ("Stock Coordinator", 2, 50000, 58000),
]
ONLINE_ROLES = [
    ("E-commerce Manager", 1, 95000, 125000),
    ("Customer Service Rep", 5, 52000, 60000),
    ("Fulfilment Associate", 6, 48000, 55000),
]
CAMPAIGN_CHANNELS = ["search", "social", "email", "display"]


def _rand_ts(rng: random.Random, day: date) -> datetime:
    # trading hours skew: 9am-9pm, peak around lunch and evening
    hour = min(21, max(8, int(rng.gauss(14.5, 3.2))))
    return datetime.combine(day, time(hour, rng.randrange(60), rng.randrange(60)))


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _stores() -> Table:
    rows: list[Row] = [
        (sid, name, city, region, CURRENCY[region], channel, opened)
        for sid, name, city, region, channel, opened in STORES
    ]
    return Table(
        "stores",
        (
            ("store_id", "integer"),
            ("store_name", "text"),
            ("city", "text"),
            ("region_code", "text"),
            ("currency_code", "text"),
            ("channel", "text"),
            ("opened_on", "date"),
        ),
        rows,
    )


def _products(rng: random.Random) -> Table:
    rows: list[Row] = []
    pid = 0
    for category, (count, lo, hi, cost_ratio) in CATEGORIES.items():
        words = PRODUCT_WORDS[category]
        for i in range(count):
            pid += 1
            prefix = rng.choice(["Kea", "Tui", "Kauri", "Harbour", "Alpine", "Coast"])
            name = f"{prefix} {words[i % len(words)]}"
            price = _money(round(rng.uniform(lo, hi)) - 0.01)
            cost = _money(price * cost_ratio * rng.uniform(0.9, 1.1))
            launched = (
                START
                - timedelta(days=rng.randrange(0, 900))
                + timedelta(days=rng.choice([0, 0, 0, 200, 400, 600]))
            )
            is_active = not (rng.random() < 0.08)
            rows.append(
                (
                    pid,
                    f"KR-{category[:3].upper()}-{pid:04d}",
                    name,
                    category,
                    price,
                    cost,
                    launched,
                    is_active,
                )
            )
    return Table(
        "products",
        (
            ("product_id", "integer"),
            ("sku", "text"),
            ("product_name", "text"),
            ("category", "text"),
            ("list_price_nzd", "numeric(10,2)"),
            ("unit_cost_nzd", "numeric(10,2)"),
            ("launched_on", "date"),
            ("is_active", "boolean"),
        ),
        rows,
    )


def _customers(rng: random.Random, n: int) -> Table:
    rows: list[Row] = []
    total_days = (END - START).days
    for cid in range(1, n + 1):
        region = "NZ" if rng.random() < 0.68 else "AU"
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        city = rng.choice(NZ_CITIES if region == "NZ" else AU_CITIES)
        handle = f"{first}.{last}".lower().replace(" ", "")
        email = f"{handle}{cid}@example.{'co.nz' if region == 'NZ' else 'com.au'}"
        if region == "NZ":
            phone = (
                f"+64 2{rng.randrange(1, 8)} {rng.randrange(100, 999)} {rng.randrange(1000, 9999)}"
            )
        else:
            phone = (
                f"+61 4{rng.randrange(10, 99)} {rng.randrange(100, 999)} {rng.randrange(100, 999)}"
            )
        # signups skew earlier so there's a returning-customer base; AU opened later
        offset = int(total_days * rng.random() ** 1.6)
        signup = START - timedelta(days=rng.randrange(0, 500)) + timedelta(days=offset)
        signup = min(signup, END)
        legacy_segment = rng.choice(["A", "B", "C", "C", None])  # deprecated, never maintained
        rows.append(
            (
                cid,
                first,
                last,
                email,
                phone,
                city,
                region,
                signup,
                rng.random() < 0.62,
                legacy_segment,
            )
        )
    return Table(
        "customers",
        (
            ("customer_id", "integer"),
            ("first_name", "text"),
            ("last_name", "text"),
            ("email", "text"),
            ("phone", "text"),
            ("city", "text"),
            ("region_code", "text"),
            ("signed_up_on", "date"),
            ("marketing_opt_in", "boolean"),
            ("legacy_segment", "text"),
        ),
        rows,
    )


def _orders_items_payments_refunds(
    rng: random.Random, customers: Table, products: Table, orders_per_day: float
) -> tuple[Table, Table, Table, Table]:
    cust_by_region: dict[str, list[tuple[int, date]]] = {"NZ": [], "AU": []}
    for c in customers.rows:
        cust_by_region[str(c[6])].append((int(str(c[0])), c[7]))  # type: ignore[arg-type]
    for lst in cust_by_region.values():
        lst.sort(key=lambda c: (c[1], c[0]))
    signups = {r: [c[1] for c in lst] for r, lst in cust_by_region.items()}
    # heavy-tailed loyalty weights: a few customers buy a lot
    weight = {int(str(c[0])): rng.paretovariate(1.6) for c in customers.rows}
    active_products = [(int(str(p[0])), float(str(p[4]))) for p in products.rows]

    orders: list[Row] = []
    items: list[Row] = []
    payments: list[Row] = []
    refunds: list[Row] = []
    oid = item_id = pay_id = ref_id = 0

    for day in _days(START, END):
        years = (day - START).days / 365.0
        expected = orders_per_day * SEASONALITY[day.month] * (1.15**years)
        if day.weekday() >= 5:
            expected *= 1.25
        if day.month == 11 and 24 <= day.day <= 30:  # Black Friday week
            expected *= 1.9
        n_orders = max(0, int(rng.gauss(expected, expected**0.5)))
        open_stores = [s for s in STORES if s[5] <= day]
        for _ in range(n_orders):
            region = "NZ" if rng.random() < 0.66 else "AU"
            # ~3% of orders come from customers registered in the other country
            cust_region = region if rng.random() >= 0.03 else ("AU" if region == "NZ" else "NZ")
            n_signed_up = bisect.bisect_right(signups[cust_region], day)
            region_stores = [s for s in open_stores if s[3] == region]
            if not n_signed_up or not region_stores:
                continue
            pool = cust_by_region[cust_region]
            sample = [pool[rng.randrange(n_signed_up)] for _ in range(min(40, n_signed_up))]
            customer_id = rng.choices([c[0] for c in sample], [weight[c[0]] for c in sample])[0]
            online = rng.random() < 0.32
            store = (
                next(s for s in region_stores if s[4] == "online")
                if online
                else rng.choice([s for s in region_stores if s[4] == "store"])
            )
            oid += 1
            ordered_at = _rand_ts(rng, day)
            currency = CURRENCY[region]
            recent = (END - day).days < 4
            r = rng.random()
            if recent and r < 0.5:
                status = "pending"
            elif r < 0.05:
                status = "cancelled"
            elif r < 0.09:
                status = "returned"
            else:
                status = "completed"
            shipping = _money(rng.choice([0.0, 0.0, 7.99, 9.99])) if online else 0.0
            discount_code = rng.choice([None] * 8 + ["SUMMER10", "WELCOME15"])

            subtotal = 0.0
            for _ in range(rng.choices([1, 2, 3, 4], [55, 27, 12, 6])[0]):
                product_id, list_price = rng.choice(active_products)
                qty = rng.choices([1, 2, 3], [85, 12, 3])[0]
                unit_price = _money(list_price * (AUD_PRICE_FACTOR if region == "AU" else 1.0))
                discount = _money(unit_price * qty * 0.10) if discount_code else 0.0
                item_id += 1
                items.append((item_id, oid, product_id, qty, unit_price, discount))
                subtotal += unit_price * qty - discount
            total = _money(subtotal + shipping)
            orders.append(
                (oid, customer_id, store[0], ordered_at, status, currency, shipping, discount_code)
            )

            if status in ("cancelled", "pending"):
                if status == "cancelled" and rng.random() < 0.3:  # failed attempt before cancel
                    pay_id += 1
                    payments.append((pay_id, oid, "card", total, "failed", ordered_at))
                continue
            paid_at = ordered_at + timedelta(minutes=rng.randrange(0, 20))
            if rng.random() < 0.03:  # failed attempt, then success
                pay_id += 1
                payments.append((pay_id, oid, "card", total, "failed", paid_at))
                paid_at += timedelta(minutes=3)
            if rng.random() < 0.10 and total > 40:  # split: gift card + card
                gift = _money(min(total - 5, rng.choice([20.0, 50.0, 100.0])))
                pay_id += 1
                payments.append((pay_id, oid, "gift_card", gift, "success", paid_at))
                pay_id += 1
                payments.append((pay_id, oid, "card", _money(total - gift), "success", paid_at))
            else:
                method = rng.choices(["card", "afterpay", "paypal", "eftpos"], [60, 15, 12, 13])[0]
                if online and method == "eftpos":
                    method = "card"
                pay_id += 1
                payments.append((pay_id, oid, method, total, "success", paid_at))

            refund_day: date | None = None
            amount = 0.0
            reason = ""
            if status == "returned":
                refund_day = day + timedelta(days=rng.randrange(3, 30))
                amount, reason = total, rng.choice(["changed_mind", "wrong_size", "faulty"])
            elif rng.random() < 0.02:
                refund_day = day + timedelta(days=rng.randrange(1, 20))
                amount, reason = _money(total * rng.uniform(0.1, 0.5)), "partial_damage"
            # planted: refunds from the old POS (before the migration) were never loaded
            if refund_day and REFUND_MIGRATION_DATE <= refund_day <= END:
                ref_id += 1
                refunds.append((ref_id, oid, _rand_ts(rng, refund_day), amount, reason))

    return (
        Table(
            "orders",
            (
                ("order_id", "integer"),
                ("customer_id", "integer"),
                ("store_id", "integer"),
                ("ordered_at", "timestamp"),
                ("status", "text"),
                ("currency_code", "text"),
                ("shipping_fee", "numeric(10,2)"),
                ("discount_code", "text"),
            ),
            orders,
        ),
        Table(
            "order_items",
            (
                ("order_item_id", "integer"),
                ("order_id", "integer"),
                ("product_id", "integer"),
                ("quantity", "integer"),
                ("unit_price", "numeric(10,2)"),
                ("discount_amount", "numeric(10,2)"),
            ),
            items,
        ),
        Table(
            "payments",
            (
                ("payment_id", "integer"),
                ("order_id", "integer"),
                ("payment_method", "text"),
                ("amount", "numeric(10,2)"),
                ("status", "text"),
                ("created_at", "timestamp"),
            ),
            payments,
        ),
        Table(
            "refunds",
            (
                ("refund_id", "integer"),
                ("order_id", "integer"),
                ("refunded_at", "timestamp"),
                ("amount", "numeric(10,2)"),
                ("reason", "text"),
            ),
            refunds,
        ),
    )


def _employees(rng: random.Random) -> Table:
    rows: list[Row] = []
    eid = 0
    for sid, _name, _city, region, channel, opened in STORES:
        roles = ONLINE_ROLES if channel == "online" else EMPLOYEE_ROLES
        manager_id: int | None = None
        for role, count, lo, hi in roles:
            for _ in range(count):
                eid += 1
                first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
                factor = 1.0 if region == "NZ" else 0.95  # AUD
                salary = round(rng.uniform(lo, hi) * factor, -2)
                hired = max(opened, START - timedelta(days=rng.randrange(0, 2500)))
                hired += timedelta(days=rng.randrange(0, 400))
                terminated = rng.random() < 0.12
                handle = f"{first}.{last}".lower().replace(" ", "")
                rows.append(
                    (
                        eid,
                        first,
                        last,
                        f"{handle}.{eid}@kiwiretail.example",
                        sid,
                        role,
                        hired,
                        None if not terminated else hired + timedelta(days=rng.randrange(120, 700)),
                        salary,
                        CURRENCY[region],
                        manager_id,
                    )
                )
                if manager_id is None:
                    manager_id = eid
    return Table(
        "employees",
        (
            ("employee_id", "integer"),
            ("first_name", "text"),
            ("last_name", "text"),
            ("work_email", "text"),
            ("store_id", "integer"),
            ("job_title", "text"),
            ("hired_on", "date"),
            ("terminated_on", "date"),
            ("annual_salary", "numeric(12,2)"),
            ("salary_currency", "text"),
            ("manager_id", "integer"),
        ),
        rows,
    )


def _marketing(rng: random.Random, customers: Table) -> tuple[Table, Table, Table]:
    campaigns: list[Row] = []
    spend: list[Row] = []
    attributions: list[Row] = []
    cid = 0
    month = date(START.year, START.month, 1)
    themes = [
        "Summer Sale",
        "Back to School",
        "Winter Warmers",
        "Black Friday",
        "Xmas Gifting",
        "Matariki",
        "Spring Refresh",
        "Brand Always-On",
    ]
    while month <= END:
        for _ in range(rng.choice([1, 1, 2])):
            cid += 1
            region = rng.choice(["NZ", "AU", "NZ"])
            channel = rng.choice(CAMPAIGN_CHANNELS)
            start = month + timedelta(days=rng.randrange(0, 20))
            length = rng.randrange(14, 45)
            end = min(start + timedelta(days=length), END)
            theme = "Black Friday" if month.month == 11 else rng.choice(themes)
            budget = round(rng.uniform(3000, 25000), -2)
            campaigns.append(
                (
                    cid,
                    f"{theme} {month.year} {region} {channel}",
                    channel,
                    region,
                    start,
                    end,
                    budget,
                    CURRENCY[region],
                )
            )
            daily = budget / ((end - start).days + 1)
            for d in _days(start, end):
                amount = _money(daily * rng.uniform(0.6, 1.4))
                impressions = int(amount * rng.uniform(80, 400))
                clicks = int(impressions * rng.uniform(0.005, 0.04))
                spend.append((cid, d, amount, impressions, clicks))
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)

    camps_by_region: dict[str, list[Row]] = {"NZ": [], "AU": []}
    for c in campaigns:
        camps_by_region[str(c[3])].append(c)
    for c in customers.rows:
        signup: date = c[7]  # type: ignore[assignment]
        live = [k for k in camps_by_region[str(c[6])] if k[4] <= signup <= k[5]]  # type: ignore[operator]
        if live and rng.random() < 0.55:
            k = rng.choice(live)
            attributions.append((c[0], k[0], signup, "first_touch"))
    return (
        Table(
            "campaigns",
            (
                ("campaign_id", "integer"),
                ("campaign_name", "text"),
                ("channel", "text"),
                ("region_code", "text"),
                ("started_on", "date"),
                ("ended_on", "date"),
                ("budget", "numeric(12,2)"),
                ("currency_code", "text"),
            ),
            campaigns,
        ),
        Table(
            "ad_spend",
            (
                ("campaign_id", "integer"),
                ("spend_date", "date"),
                ("spend", "numeric(12,2)"),
                ("impressions", "integer"),
                ("clicks", "integer"),
            ),
            spend,
        ),
        Table(
            "attributions",
            (
                ("customer_id", "integer"),
                ("campaign_id", "integer"),
                ("attributed_on", "date"),
                ("model", "text"),
            ),
            attributions,
        ),
    )


def generate(
    seed: int = DEFAULT_SEED, *, customers: int = 9000, orders_per_day: float = 22.0
) -> list[Table]:
    """All raw tables, in load order. Pure and deterministic for a given seed."""
    rng = random.Random(seed)
    stores = _stores()
    products = _products(rng)
    custs = _customers(rng, customers)
    orders, items, payments, refunds = _orders_items_payments_refunds(
        rng, custs, products, orders_per_day
    )
    employees = _employees(rng)
    campaigns, ad_spend, attributions = _marketing(rng, custs)
    return [
        stores,
        products,
        custs,
        orders,
        items,
        payments,
        refunds,
        employees,
        campaigns,
        ad_spend,
        attributions,
    ]


def fingerprint(tables: Sequence[Table]) -> str:
    """Stable hash of all generated content (used to prove determinism)."""
    h = hashlib.sha256()
    for t in tables:
        h.update(json.dumps([t.name, t.columns, t.rows], default=str).encode())
    return h.hexdigest()
