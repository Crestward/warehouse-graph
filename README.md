# warehouse-graph

Shared foundation for two linked projects:

- [`warehouse-qa`](https://github.com/Crestward/warehouse-qa): a governed, lineage-aware "ask your warehouse" assistant
- [`pipeline-sentinel`](https://github.com/Crestward/pipeline-sentinel): a tiered incident agent for data pipelines

This package will build a metadata/lineage graph from dbt artifacts, compile one `policies.yml` to database access controls (Postgres RLS / Unity Catalog), and serve the graph over an MCP server.

> **Status: Phase 1 (demo warehouse).** Done so far:
> - repo tooling and the LLM wrapper (cost ledger, cache, budget cap, Batch API)
> - **Kiwi Retail Co.**, a deliberately messy synthetic warehouse: 40 dbt models, 13 metrics, 6 dashboards, 13 documented traps

## Quick start

```bash
cp .env.example .env      # add ANTHROPIC_API_KEY for real model calls
uv sync
make warehouse            # Postgres + synthetic data + dbt build + artifacts (~1 min)
make demo                 # the same, then the Dagster UI at http://localhost:3000
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

## Docs
- [`docs/design-decisions.md`](docs/design-decisions.md): decisions and why
- [`docs/planted-traps.md`](docs/planted-traps.md): the 13 traps the evals are built around
- [`docs/parking-lot.md`](docs/parking-lot.md): ideas deliberately left out of v1

## Licence
MIT. See [LICENSE](LICENSE).
