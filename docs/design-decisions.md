# Design decisions

Format: **context → options → choice → why**. Newest at the bottom. Scope swaps (PRD §1.4.5 rule 2) are recorded here too.

## D1. dbt project source (task 0.7 in v1.1, closed in PRD v1.2)
- **Context:** the demo warehouse needs enough lineage depth to matter.
- **Options:** adapt a large public dbt project (e.g. GitLab's) and synthesise data for it; or write our own.
- **Choice:** self-authored, 40 models (hard cap 50).
- **Why:** trimming a 1,000+ model project and generating data for its schema is a time sink. A purpose-built project lets us plant traps deliberately (deprecated models, stale sources, duplicate metrics, fan-out joins).

## D2. Orchestrator
- **Options:** Dagster or Airflow.
- **Choice:** Dagster, and only Dagster.
- **Why:** faster local dev, first-class dbt integration and run-failure hooks. The cloud profile uses Lakeflow Jobs either way, and the `EventSource` adapter keeps this swappable later.

## D3. UI scope
- **Choice:** P1 gets a CLI + one Streamlit page. P2 gets console approval locally, and a Teams card + Entra-protected approval page in `azure`. No web console, no hosted P1 app.
- **Why:** reviewers need to see it work, not a product UI. Hosting needs auth, which is scope creep (PRD §1.4.4).

## D4. LLM vendor and cost strategy
- **Choice:** Claude only (Sonnet 5.5 default, Haiku 4.5 for cheap subtasks), with cost engineering instead of a cheaper vendor. Hard cap US$150.
- **Why:** see PRD §5. A cheaper third-party API saves about US$30–40 after the levers, but it hurts the governance/data-residency story and would turn baseline B1 into a strawman.

## D5. LLM wrapper design (task 0.5)
- **Context:** every model call must be costed, cached where possible and capped.
- **Choice:**
  - one `LLMProvider` protocol (`AnthropicProvider` + `FakeProvider`)
  - one `LLMClient` that adds a response cache, a cost ledger (`costs.csv`) and a budget cap across all runs
  - `complete_many(batch=True)` for Batch API rounds
- **Details:**
  - The system prompt always carries an explicit cache breakpoint; agent loops set `cache_messages=True` for automatic caching of the growing tail. Tools are sorted by name so the prefix stays byte-stable.
  - Retries use the SDK's built-in backoff (`max_retries=4`). We don't add a second retry layer.
  - Response-cache hits are logged at $0, so `docs/cost.md` can report the cache's savings.
  - When a batch partially fails, successful items are recorded and cached before re-raising, so retries never pay twice.
  - Pinned to `anthropic>=2.1,<3` (API version 2026-10-21; `temperature`/`top_p`/`top_k` are rejected, so the wrapper never sends them).
  - Cache-read prices are conservative where sources disagree, so the cap errs on stopping early.

## D6. Licence and dependency wiring
- **Choice:** MIT for all three repos. `warehouse-qa` and `pipeline-sentinel` depend on `warehouse-graph` as a git dependency pinned to a tag, not published to PyPI.
- **Why:** simplest permissive licence for a portfolio; a git tag pin keeps the three repos reproducible without a package registry.

## D7. Cloud choice confirmed by market evidence (task 0.6)
- **Evidence:** the NZ AI/ML ads I'd apply for almost all say "cloud-native engineering on Azure, AWS, or GCP; Python; modern MLOps tooling" (`docs/market-evidence.md`).
- **Choice:** keep Azure + Azure Databricks (PRD §6.1). No swap to AWS.
- **Why:** the ads are cloud-agnostic, so depth on one cloud is what counts. Azure leads the NZ data-platform sample, and Unity Catalog row filters / masks map directly onto policy-as-code. The adapter design keeps a later AWS port limited to PRD §6.6–6.7.
- **MLOps:** v1 already covers the practices employers mean (CI gates, eval gates, IaC, monitoring, cost controls). MLflow tracking was parked rather than added (scope contract, swap never add).
