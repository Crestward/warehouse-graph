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
| 1 | `dbt-databricks` connects and builds a model | throwaway dbt project, one model `select 1 as id`, `dbt build` | ☐ | none: this one must work |
| 2 | Row filter keyed on `current_user()` works | create a table with a `region` column; `CREATE FUNCTION ... RETURN current_user() = '<you>' OR region = 'AU'`; `ALTER TABLE ... SET ROW FILTER`; query it | ☐ | none: Unity Catalog governance is the core cloud claim |
| 3 | Extra workspace users can be added | **Settings → Identity and access → Users → Add** a second email you control; log in and query the filtered table | ☐ | multi-role tests run only in the `azure` profile (Phase 16); `databricks-dev` covers one role (PRD §6.4) |
| 4 | Azure Databricks serverless SQL in **New Zealand North** | Azure portal / docs region table (no Azure spend needed) | ☐ | deploy to **Australia East**; record the reason |

## Outcome
- Identity approach chosen for Phase 9:
- Region for Phase 16:
- Anything surprising:
