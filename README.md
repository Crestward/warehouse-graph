# warehouse-graph

Shared foundation for two linked projects:

- [`warehouse-qa`](https://github.com/Crestward/warehouse-qa): a governed, lineage-aware "ask your warehouse" assistant
- [`pipeline-sentinel`](https://github.com/Crestward/pipeline-sentinel): a tiered incident agent for data pipelines

This package will build a metadata/lineage graph from dbt artifacts, compile one `policies.yml` to database access controls (Postgres RLS / Unity Catalog), and serve the graph over an MCP server.

> **Status: Phase 0 (setup).** So far: repo tooling, local Postgres, and the LLM wrapper that every model call goes through (response cache, cost ledger, hard budget cap, Batch API rounds, prompt caching).

## Quick start

```bash
cp .env.example .env      # add ANTHROPIC_API_KEY for real model calls
uv sync
make up                   # Postgres 16 via Docker Compose
make check                # lint, format check, type-check, tests
make llm-smoke            # one cheap real call; logs a row to costs.csv
```

## Docs
- [`docs/design-decisions.md`](docs/design-decisions.md): decisions and why
- [`docs/parking-lot.md`](docs/parking-lot.md): ideas deliberately left out of v1

## Licence
MIT. See [LICENSE](LICENSE).
