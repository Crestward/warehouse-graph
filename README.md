# warehouse-graph

Shared foundation for two linked projects:

- [`warehouse-qa`](https://github.com/Crestward/warehouse-qa): a governed, lineage-aware "ask your warehouse" assistant
- [`pipeline-sentinel`](https://github.com/Crestward/pipeline-sentinel): a tiered incident agent for data pipelines

This package will build a metadata/lineage graph from dbt artifacts, compile one `policies.yml` to database access controls (Postgres RLS / Unity Catalog), and serve the graph over an MCP server.

> **Status: Phase 2 (graph core).** Done so far:
> - repo tooling and the LLM wrapper (cost ledger, cache, budget cap, Batch API)
> - **Kiwi Retail Co.**, a deliberately messy synthetic warehouse: 40 dbt models, 13 metrics, 6 dashboards, 13 documented traps
> - **the metadata graph**: 574 nodes and 1,025 edges built from the dbt artifacts in ~10 s, with 97.8% column-level lineage and a Python query API

## Quick start

```bash
cp .env.example .env      # add ANTHROPIC_API_KEY for real model calls
uv sync
make warehouse            # Postgres + synthetic data + dbt build + artifacts (~1 min)
make graph                # make warehouse, then build the graph file (build/graph.wgz)
make demo                 # make warehouse, then the Dagster UI at http://localhost:3000
make check                # lint, format check, type-check, tests
```

## The demo warehouse: Kiwi Retail Co.
A fictional NZ/AU retailer: 12 stores (2 online), 9,000 customers, ~29k orders from Jan 2024 to Sep 2026, in NZD and AUD.

```
raw.* (11 tables, seeded generator)
  └─ staging (11 views) ─ intermediate (9 views) ─ marts (20 tables)
                                                    ├─ core: customers, products, stores, employees, orders, items, payments, refunds
                                                    ├─ finance: fct_revenue_daily (canonical revenue), rpt_revenue_by_region
                                                    ├─ marketing: campaigns, ad spend, campaign performance
                                                    ├─ customers: churn, LTV, active customers
                                                    └─ legacy: orders_v1 (deprecated), rpt_regional_sales_legacy (fan-out)
```

| Piece | Where |
|---|---|
| Generator and loader | `src/warehouse_graph/demo/` (`warehouse-graph demo-data [--stale TABLE]`) |
| dbt project (models, tests, freshness, exposures, Semantic Layer metrics) | `demo/kiwi_retail/` |
| Tribal knowledge attached to nodes | `demo/caveats.yml` (14 caveats) |
| Validated question → SQL examples | `demo/query_examples.yml` (30, each tested against the DB) |
| Orchestration (nightly `dbt build`, run-failure sensor) | `demo/orchestration/definitions.py` (Dagster) |
| Artifacts for the graph | `demo/artifacts/` (`manifest`, `catalog`, `run_results`, `sources`), produced by `make warehouse` |
| Planted traps and their measured effects | [`docs/planted-traps.md`](docs/planted-traps.md) |

## The metadata graph
`warehouse-graph build` turns the four dbt artifacts, `caveats.yml` and `query_examples.yml` into one versioned file, `build/graph.wgz`. It holds a NetworkX graph, a local sentence-transformer embedding per node, and the manifest hash. It loads into memory in under a second and needs no database.

| Nodes | Edges |
|---|---|
| `source` · `model` · `column` · `metric` · `exposure` · `caveat` · `query_example` | `DEPENDS_ON` · `HAS_COLUMN` · `COLUMN_DERIVES_FROM` (sqlglot) · `DEFINES` · `EXPOSES` · `HAS_CAVEAT` · `ANSWERED_BY` |

Every node carries the governance metadata later phases need: owner, deprecation + replacement, planted-trap flags, and PII/sensitivity tags, which spread along pass-through column lineage down to the raw tables. Counts and lineage coverage are in [`docs/graph-stats.md`](docs/graph-stats.md).

```python
from pathlib import Path
from warehouse_graph.graph import GraphArtifact, GraphQuery

q = GraphQuery(GraphArtifact.load(Path("build/graph.wgz")))
q.search_nodes("churn rate", k=5)  # hybrid BM25 + vector, reciprocal-rank fused
q.get_node("model.orders_v1")  # record + caveats + columns (deprecated -> fct_orders)
q.get_lineage("column.fct_revenue_daily.net_revenue_nzd", "upstream", depth=10, level="column")
q.get_blast_radius("source.shop.orders")  # downstream models, dashboards, metrics by criticality
q.get_metric("revenue")  # definition + SQL; canonical_alternative = net_revenue
```
These five operations become the five MCP tools in Phase 3.

Lineage of the canonical revenue model (`warehouse-graph lineage model.fct_revenue_daily --direction both`):

```mermaid
flowchart LR
    model_fct_revenue_daily["fct_revenue_daily"]:::model
    exposure_executive_revenue_dashboard[/"executive_revenue_dashboard"/]:::exposure
    metric_gross_revenue{{"gross_revenue"}}:::metric
    metric_net_revenue{{"net_revenue"}}:::metric
    model_fct_orders["fct_orders"]:::model
    model_fct_refunds["fct_refunds"]:::model
    model_rpt_revenue_by_region["rpt_revenue_by_region"]:::model
    model_int_orders__enriched["int_orders__enriched"]:::model
    model_int_orders__item_totals["int_orders__item_totals"]:::model
    model_int_payments__by_order["int_payments__by_order"]:::model
    model_int_refunds__by_order["int_refunds__by_order"]:::model
    model_stg_shop__refunds["stg_shop__refunds"]:::model
    model_int_order_items__priced["int_order_items__priced"]:::model
    model_stg_shop__orders["stg_shop__orders"]:::model
    model_stg_shop__payments["stg_shop__payments"]:::model
    model_stg_shop__stores["stg_shop__stores"]:::model
    source_shop_refunds[("shop.refunds")]:::source
    model_stg_shop__order_items["stg_shop__order_items"]:::model
    model_stg_shop__products["stg_shop__products"]:::model
    source_shop_orders[("shop.orders")]:::source
    source_shop_payments[("shop.payments")]:::source
    source_shop_stores[("shop.stores")]:::source
    source_shop_order_items[("shop.order_items")]:::source
    source_shop_products[("shop.products")]:::source
    model_fct_revenue_daily --> exposure_executive_revenue_dashboard
    model_rpt_revenue_by_region --> exposure_executive_revenue_dashboard
    model_fct_revenue_daily --> metric_gross_revenue
    model_fct_revenue_daily --> metric_net_revenue
    model_int_orders__enriched --> model_fct_orders
    model_int_orders__item_totals --> model_fct_orders
    model_int_payments__by_order --> model_fct_orders
    model_int_refunds__by_order --> model_fct_orders
    model_int_orders__enriched --> model_fct_refunds
    model_stg_shop__refunds --> model_fct_refunds
    model_fct_orders --> model_fct_revenue_daily
    model_fct_refunds --> model_fct_revenue_daily
    model_stg_shop__order_items --> model_int_order_items__priced
    model_stg_shop__orders --> model_int_order_items__priced
    model_stg_shop__products --> model_int_order_items__priced
    model_stg_shop__orders --> model_int_orders__enriched
    model_stg_shop__stores --> model_int_orders__enriched
    model_int_order_items__priced --> model_int_orders__item_totals
    model_stg_shop__payments --> model_int_payments__by_order
    model_stg_shop__refunds --> model_int_refunds__by_order
    model_fct_revenue_daily --> model_rpt_revenue_by_region
    source_shop_order_items --> model_stg_shop__order_items
    source_shop_orders --> model_stg_shop__orders
    source_shop_payments --> model_stg_shop__payments
    source_shop_products --> model_stg_shop__products
    source_shop_refunds --> model_stg_shop__refunds
    source_shop_stores --> model_stg_shop__stores
    classDef source fill:#e8f0fe,stroke:#4a6fa5
    classDef model fill:#f4f4f4,stroke:#666
    classDef metric fill:#e6f4ea,stroke:#3c8d5a
    classDef exposure fill:#fff4e5,stroke:#c77700
    classDef column fill:#fafafa,stroke:#999
    classDef flagged stroke:#c62828,stroke-width:2px,stroke-dasharray:4 2
```

## Docs
- [`docs/design-decisions.md`](docs/design-decisions.md): decisions and why
- [`docs/planted-traps.md`](docs/planted-traps.md): the 13 traps the evals are built around
- [`docs/graph-stats.md`](docs/graph-stats.md): node/edge counts and column-lineage coverage
- [`docs/parking-lot.md`](docs/parking-lot.md): ideas deliberately left out of v1

## Licence
MIT. See [LICENSE](LICENSE).
