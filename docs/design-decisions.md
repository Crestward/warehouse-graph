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

## D8. Azure region: Australia East (task 0.7, check 4)
- **Evidence:** Azure Databricks isn't offered in New Zealand North (Microsoft Learn region list, updated 2026-10-21). Australia East has serverless SQL warehouses, Unity Catalog, system tables and Model Serving.
- **Choice:** the whole `azure` profile (Databricks, storage/queue, Function App, Key Vault, App Insights) goes in **Australia East**.
- **Why one region:** splitting the Functions into NZ North would add cross-region latency and egress without any residency gain, because the data lives in Databricks. Australia East is the nearest Databricks region to NZ. Re-check before Phase 16 in case NZ North is added.

## D9. Demo warehouse design (Phase 1)
- **Data:** a seeded, pure-Python generator (`warehouse_graph.demo.datagen`).
  - Fixed date range 2024-01-01 → 2026-09-30, about 125k rows, so eval answers never drift.
  - Only `_loaded_at` is set at load time, so source freshness works against "now", and `--stale` back-dates it on demand (the P2 `stale_source` fault).
  - *Rejected:* Faker or a sample dataset. Both are extra dependencies or licences, and neither is deterministic across versions.
- **Size:** 9,000 customers and about 29k orders. The full `dbt build` (40 models, 115 tests) takes about 5 s locally, well inside the < 2 min budget that Phase 10 needs for its per-incident rebuilds.
- **Metrics:** the dbt Semantic Layer (semantic models + metrics in `models/semantic/metrics.yml`) rather than a home-made YAML file. The metrics land in `manifest.json` next to everything else, and it's the format employers use. Ratio metrics need simple helper metrics; those are marked `meta.helper`.
- **Portability:** date and string logic uses dbt's cross-database macros (`dbt.datediff`, `dbt.date_trunc`, `dbt.concat`, `dbt.type_*`). The same project should then build on Databricks in Phase 9 with no SQL changes.
- **Artifacts:** `warehouse-graph demo-build` runs build → freshness → docs and copies each artifact as soon as it's written to `demo/artifacts/`, because dbt overwrites `run_results.json` on every command.
- **Dependencies:** dbt and Dagster are in a `demo` dependency group, installed by default for this repo but never pulled in by `warehouse-qa` or `pipeline-sentinel`. `psycopg` and `PyYAML` are core dependencies, because Phases 2–3 need them anyway.
- **Traps:** documented in `docs/planted-traps.md` with measured effect sizes, and flagged in dbt `meta` so the graph can see them.

## D10. Graph storage, ids and search (Phase 2)
- **Storage:** one zip file (`build/graph.wgz`) with `meta.json`, `graph.json` and `embeddings.npy`, loaded into an in-memory NetworkX `MultiDiGraph`.
  - No pickle, so loading a graph file can't run code. A `format_version` makes an old file fail loudly ("rebuild it") instead of half-loading.
  - The meta records the manifest sha256, a hash over all inputs, the embedding model and the build stats.
- **Vectors:** brute-force NumPy dot products, not FAISS. With 574 nodes an exact search takes microseconds, so an index would only add a dependency. FAISS stays in the PRD's stack as an option if the graph ever grows by 100×.
- **Embeddings:** `sentence-transformers/all-MiniLM-L6-v2` on CPU (384 dims, ~90 MB, ~1 s for the whole graph), set in `warehouse-graph.toml`. Torch comes from the PyTorch CPU index, so CI doesn't download CUDA. Tests use a hashing embedder and need no download.
- **Hybrid ranking:** BM25 (implemented in ~20 lines, no dependency) and cosine similarity, fused by **reciprocal rank fusion** (k = 60). RRF needs no score normalisation or weight tuning, which keeps tuning out of Phase 2. Phase 5 measures retrieval and may change this.
- **Ids:** short and readable (`model.fct_orders`, `column.fct_orders.order_total_nzd`, `source.shop.orders`), not dbt's `model.kiwi_retail.fct_orders`. They're what the LLM reads and quotes back, so they cost fewer tokens and can't be confused across projects. They match the reference format in `caveats.yml`.
- **Neo4j/Kùzu/pgvector:** not used (PRD §2.2). The graph is small and read-only at runtime, so a file artifact works in every profile with nothing to host.

## D11. Column lineage and PII propagation (tasks 2.3–2.4)
- **Lineage:** sqlglot's `lineage()` over each model's compiled SQL, qualified against the catalog schema. It took well under the 1-day time box: **97.8%** of model columns (363/371) trace upstream, with no model failing to parse and no hand fixes. The 8 untraced columns are all `count(*)`-style aggregates or the time spine's generated dates, which genuinely have no upstream column.
- **PII propagation:** dbt sources declare no columns, so raw columns such as `shop.customers.email` had no PII tag, and role-filtered metadata (Phase 3) would have leaked them. Tags now spread along `COLUMN_DERIVES_FROM` edges **between same-named columns only** (pass-throughs), in both directions. A computed column (e.g. `has_email`) isn't tagged just because it reads a PII column. The quality tests check that every name/email/phone/salary column is tagged from raw to marts.
- **Rejected:** tagging by column name pattern alone. It's simple, but it misses renamed PII and silently tags unrelated columns.

## D12. Mermaid instead of a PNG for the lineage picture (task 2.8)
- **Context:** task 2.8 asks for one rendered subgraph as a PNG for the README.
- **Choice:** `warehouse-graph lineage <node> --direction both` prints a Mermaid flowchart, pasted into the README, where GitHub renders it.
- **Why:** a PNG needs matplotlib or Graphviz, which is a new dependency for one picture (PRD §7: adding to the stack is a scope change). Mermaid is text, so it diffs and can be regenerated from the artifact. It's the same size of task or smaller (§1.4.5 rule 2: a swap, not an addition).

## D13. Postgres policy layout: masked views over RLS tables (task 3.3)
- **Problem:** Postgres has RLS and column grants but no column masking. Masking needs a view, and a masking view has to read the raw column, so the view can't run with the caller's rights (`security_invoker`). If it did, the caller would need a grant on the raw value, and could read it directly.
- **Choice:**
  - `warehouse-graph policies` moves each table dbt built in `marts` to `marts_base`, and puts a `security_barrier` view with the same name in `marts`.
  - The view is owned by `policy_owner` (no login, not superuser, not the table owner). It masks with `CASE WHEN current_user IN (...)`.
  - Row filters are real **RLS policies** on the `marts_base` tables, written for `policy_owner` and keyed on `current_user`. Inside a view, `current_user` is still the *caller*, which I checked against Postgres before building on it.
  - Principals have no USAGE on `marts_base`, and get **column-level** SELECT on the views. A hidden column is simply never granted.
  - Each principal is its own LOGIN role: NOINHERIT, not a member of anything, read-only by default, 30 s statement timeout.
- **Why:**
  - The SQL an LLM writes is `marts.<table>` for every role. It means the same thing on Unity Catalog (Phase 9), where native row filters and column masks sit on the table itself. Gold SQL and eval questions don't fork per backend.
  - Two independent controls protect a row: the view's owner is subject to RLS, and the caller can't reach the base table at all.
- **Rejected:**
  - *Generated `email_masked` columns:* the column names would differ per role and per backend.
  - *Per-role schemas with `search_path`:* the LLM's SQL would have to change per role.
  - *Predicates inline in each view:* no RLS at all, and the PRD's control matrix asks for it.
- **Consequences:**
  - dbt replaces the tables on every build, dropping the views, so access **fails closed** until `make policies` runs again. The script is idempotent: it revokes everything, then re-grants.
  - Tables that can't be filtered (no `region_code`, e.g. `orders_v1`) are *denied* to row-filtered roles unless listed as `unfiltered`. That's deny by default.
  - Masking only exists in `marts`. A masked column elsewhere is hidden instead (fail closed).
  - Trap evals that need `orders_v1` must use a role without a row filter.
  - The `audit_writer` role is the one login role that isn't read-only by default. It has an INSERT-only grant instead. The first DB test run caught this: it had been created read-only like the principals and couldn't write.

## D14. Role-filtered metadata and the description lint (tasks 3.5-3.6)
- **One decision, two enforcers:** `policies.yml` is resolved once against the graph's tags into a `PolicySpec`. The same spec drives the SQL compiler and the metadata filter, so "what the model can see" and "what the database allows" can't drift apart.
- **What filtering removes for each principal:**
  - relations it can't query, and hidden columns
  - metrics and query examples that would reveal them
  - caveats and exposures left with nothing visible to attach to
  - compiled model SQL, for anyone who doesn't see everything, because it names every upstream column
- Relations that lost columns are **re-embedded**, because their original text listed the hidden column names and the vector would still "know" them.
- **Non-confirming:** a hidden id returns exactly the same "unknown node id" error as a made-up id, through the query API and the MCP tools (tested both ways).
- **Lint:**
  - Visible text is flagged if it names a column hidden from that principal, its name with spaces, or its PII kind (`salary`, `email`, `phone`). `name` alone is too common to flag.
  - It is scoped to the tables the text describes. "Retention campaigns (especially email)" on a campaign caveat is about a channel, so it isn't flagged.
  - The first run found real leaks in the dbt docs, e.g. "Contains PII (name, email, phone)" on `dim_customers` (visible to marketing) and "with annual salary" on `dim_employees`. The descriptions were rewritten to point at column tags instead, and the payroll caveat now attaches only to the salary column.
  - `warehouse-graph policies` refuses to apply while the lint has findings.

## D15. MCP server on SDK v2, identity binding, pinned schemas (tasks 3.7-3.9)
- **SDK:** the official Python SDK is now at 2.x, where `FastMCP` became `MCPServer`. Tool functions return the Pydantic models from the query API, so every tool has a structured output schema.
- **Identity:**
  - Over an authenticated transport, the access token's subject is the identity.
  - Over stdio there's no token, so the host app, which has already authenticated its user, starts the server bound to that identity (`warehouse-graph mcp --identity`).
  - Either way `policies.yml` maps the identity to a principal. Unknown identities are refused at startup, and a server with no identity answers nothing.
- **Schemas:** `schemas/mcp/v1/` pins every tool's input and output JSON Schema, and contract tests fail on drift. Changing them is a deliberate act (`warehouse-graph mcp-schemas --write`) and means a schema version bump for clients.
- **Audit:** `audit.events` is append-only by trigger (UPDATE, DELETE and TRUNCATE are rejected, even for the owner). Only `audit_writer` (INSERT only) writes, and only `admin` reads. MCP tool calls and not-found lookups are logged when the server runs with `--audit`.
- **Additive change (2026-10-26, for P1 retriever 2):** `search_nodes` gained an optional `mode` (`hybrid` default, `vector`, `bm25`) so a single-signal baseline goes through the same governed tool. Existing calls are unchanged; the pinned input schema was regenerated deliberately. Still exactly 5 tools.
