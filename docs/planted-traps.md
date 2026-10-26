# Planted traps in the demo warehouse

"Kiwi Retail Co." is deliberately messy, the way real warehouses are. Each trap below is a place where a naive analyst, or a naive text-to-SQL system, gets a wrong answer that *looks* right. The traps are what later phases are measured against:
- **P1 trap questions**: Phase 4 writes 40 eval questions aimed at these.
- **P1 sanity checks**: PRD §3.5, Phase 6.
- **P2 fault scenarios**: PRD §4.4, Phase 10.

Every trap is flagged in dbt metadata, so the graph (Phase 2) can see it:
- `meta.trap: {kind, note, use_instead}` on models, columns and metrics
- `meta.deprecated` + `meta.replaced_by` for deprecations
- `meta.canonical` on metrics

Most traps also have a caveat in `demo/caveats.yml`. `tests/test_demo_assets.py` fails if a flag goes missing.

Effect sizes were measured on the default build (seed 42, as of 2026-09-30).

| # | Trap | Kind | Where | Effect if you fall for it | Correct path | Caveat | Used by |
|---|---|---|---|---|---|---|---|
| T1 | Deprecated model | `deprecated_model` | `orders_v1` (`replaced_by: fct_orders`) | Sums every payment attempt, including failed ones, and ignores currency. Completed-order totals come out **+3.1%**, and AUD is treated as NZD | `fct_orders` | `cav_orders_v1_deprecated` | P1 deprecated-usage check; trap questions |
| T2 | Deprecated column | `deprecated_column` | `dim_customers.legacy_segment` (also in staging) | 2019 A/B/C segment, never updated, **20% null** | `dim_customers.customer_segment` | `cav_legacy_segment` | P1 deprecated-usage check |
| T3 | Stale-able source | freshness SLA | `source.marketing.ad_spend` (warn 24 h, error 48 h; `meta.stale_on_demand`) | `warehouse-graph demo-data --stale ad_spend` back-dates `_loaded_at` by 3 days, and `dbt source freshness` reports **ERROR STALE** | answer with a staleness warning | `cav_ad_spend_vendor_delay` | P1 staleness check; P2 `stale_source` |
| T4 | Competing "revenue" | `competing_metric` | metric `revenue` (non-canonical, `superseded_by: net_revenue`) over `fct_sales` | Mixes NZD and AUD, includes cancelled and pending orders, excludes shipping and refunds. **+10.4%** vs net revenue overall | metric `net_revenue` / `fct_revenue_daily` | `cav_fct_sales_not_revenue` | P1 trap questions ("what was revenue…") |
| T5 | Competing "churn" | `competing_metric` | metric `churn_rate_marketing` / column `is_churned_marketing` | 180-day window, and never-ordered customers count as churned: **63.0%** vs the canonical **73.3%**, so a different answer to the same question | metric `churn_rate` / `is_churned` | `cav_two_churn_definitions` | P1 clarify/abstain questions |
| T6 | Fan-out join (wrong key) | `fanout_join` | `rpt_regional_sales_legacy` joins orders to stores on `region_code` instead of `store_id` | Each order counted once per store in its region: totals inflated **6.36×** | `rpt_revenue_by_region` | `cav_regional_sales_legacy_inflated` | P1 join fan-out check; blast radius (feeds a dashboard) |
| T7 | Fan-out bait | `fanout_join` | `fct_payments` / `stg_shop__payments`: several rows per order (split tenders on **9.4%** of revenue orders, failed attempts on **4.4%** of orders) | Joining orders to payments on `order_id` and summing order totals inflates revenue **+12.8%** | `fct_orders.paid_amount` | `cav_payments_multiple_rows` | P1 join fan-out check; trap questions |
| T8 | Misleading name | `misleading_name` | `fct_sales` | Sounds like the sales fact; is line-level, local currency, all statuses | `fct_revenue_daily` | `cav_fct_sales_not_revenue` | P1 retrieval traps |
| T9 | Misleading name | `misleading_name` | `rpt_active_customers` | Has **all 9,000** customers; only **3,331** are active | filter `is_active` | `cav_active_customers_all_rows` | P1 trap questions ("how many active customers") |
| T10 | Data gap | caveat only | `source.shop.refunds` starts 2024-03-01 (old POS refunds never migrated) | Jan–Feb 2024 net revenue overstated; year-on-year comparisons that include those months are skewed | compare from March 2024, or use gross | `cav_refunds_before_2024_03` | P1 caveat surfacing |
| T11 | Currency | caveat only | raw amounts are NZD **or** AUD; marts convert at a fixed 1.09 | Summing `order_total` (not `_nzd`) adds AUD to NZD; NZD figures differ slightly from statutory accounts | `*_nzd` columns | `cav_fixed_fx_rate` | P1 trap questions |
| T12 | Ambiguous dimension | caveat only | `fct_orders.region_code` (store) vs `dim_customers.region_code` (home); **2.8%** of orders differ | "Revenue by region" changes depending on which region is used | order region | `cav_order_region_vs_customer_region` | P1 clarify questions |
| T13 | Moving "now" | caveat only | churn, activity and tenure use `var('as_of_date')` = 2026-09-30, not `current_date` | Re-deriving "last 90 days" from today gives different numbers | use `as_of_date` | `cav_as_of_date` | P1 trap questions |

The `tasks.md` 1.4 checklist maps onto these as follows:

| Checklist item | Trap |
|---|---|
| 2 deprecated models/columns | T1, T2 |
| 1 stale-able source | T3 |
| 2 competing metric definitions | T4, T5 |
| 1 wrong-key fan-out join | T6 |
| misleadingly named tables | T8, T9 |

T7 and T10–T13 come for free from realistic data.

## Reproducing the numbers
```bash
make warehouse
docker compose exec postgres psql -U warehouse -d warehouse
```
Then, for example, the T6 ratio:
```sql
select (select sum(total_sales_nzd) from marts.rpt_regional_sales_legacy)
     / (select sum(gross_revenue_nzd) from marts.fct_revenue_daily);
```
