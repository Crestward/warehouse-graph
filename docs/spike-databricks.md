# Databricks Free Edition spike (task 0.7, half a day, toy data)

Goal: find the platform limits now, not in week 11. Record each result and the fallback taken.

## Setup
1. Sign up at Databricks Free Edition (free, no card). Note the workspace URL.
2. In the workspace: **Settings → Developer → Access tokens → Generate** (lifetime: 30 days). Put the URL and token in `.env` as `DATABRICKS_HOST` and `DATABRICKS_TOKEN` (never commit).
3. Note the SQL warehouse's HTTP path (**SQL Warehouses → your warehouse → Connection details**) as `DATABRICKS_HTTP_PATH`.

**Connection test (2026-10-26): passed.** `databricks-sql-connector` → Serverless Starter Warehouse: `SELECT 1` works, default catalog `workspace`, catalogs visible `samples`, `system`, `workspace`. First connect took about 15 s (warehouse cold start).
- Gotcha: `DATABRICKS_HOST` must be the bare `https://dbc-….cloud.databricks.com`. The browser URL's `/?o=<workspace-id>` suffix makes the connection fail with a generic `RequestError`.

## Checks
| # | Check | How | Result | Fallback if it fails |
|---|---|---|---|---|
| 1 | `dbt-databricks` connects and builds a model | throwaway dbt project, one model `select 1 as id`, `dbt build` | ✅ 2026-10-26: dbt-core 1.12.5 + dbt-databricks 1.12.8 built a table in `workspace.spike_dbt` plus 2 tests (PASS=3, 24 s). Gotcha: in Git Bash set `MSYS_NO_PATHCONV=1`, or `/sql/1.0/...` in the HTTP path gets rewritten to a Windows path (HTTP 404 on OpenSession) | none: this one must work |
| 2 | Row filter keyed on `current_user()` works | create a table with a `region` column; `CREATE FUNCTION ... RETURN current_user() = '<you>' OR region = 'AU'`; `ALTER TABLE ... SET ROW FILTER`; query it | ✅ 2026-10-26: filter keyed on me → 3/3 rows; same filter keyed on another user → only the AU row. Column mask on `email` → `***`. Holds for `count(*)` (1) and through a view. Test schemas dropped afterwards | none: Unity Catalog governance is the core cloud claim |
| 3 | Extra workspace users can be added | **Settings → Identity and access → Users → Add** a second email you control; log in and query the filtered table | ☐ **you**: needs a second email you control | multi-role tests run only in the `azure` profile (Phase 16); `databricks-dev` covers one role (PRD §6.4) |
| 4 | Azure Databricks serverless SQL in **New Zealand North** | Azure portal / docs region table (no Azure spend needed) | ❌ 2026-10-26: `newzealandnorth` isn't an Azure Databricks region at all (Microsoft Learn region list, updated 2026-10-21). `australiaeast` has serverless SQL, Model Serving / MCP and system tables → fallback taken | deploy to **Australia East**; record the reason |

## Outcome
- Identity approach chosen for Phase 9: _pending check 3._ The row-filter and mask mechanics are proven for one user (check 2); a second user decides whether multi-role tests run on `databricks-dev` or only in `azure`.
- Region for Phase 16: **Australia East** for the whole `azure` profile (one region: no cross-region traffic, one Terraform location). Reason: Azure Databricks isn't offered in New Zealand North. Recorded as D8.
- Anything surprising: two Windows/setup gotchas (`/?o=` suffix on the host; Git Bash path conversion of the HTTP path). Cold start of the starter warehouse is about 15 s.

Sources: https://learn.microsoft.com/en-us/azure/databricks/resources/supported-regions · https://learn.microsoft.com/en-us/azure/databricks/resources/feature-region-support
