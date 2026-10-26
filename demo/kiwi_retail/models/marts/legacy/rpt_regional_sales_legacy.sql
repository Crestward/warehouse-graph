-- Planted trap (fan-out): joins orders to stores on region_code instead of store_id, so every
-- order is repeated once per store in its region (x7 NZ, x5 AU). Totals are inflated.
-- Still feeds the "Regional Sales (legacy)" dashboard.
select
    cast({{ dbt.date_trunc('month', 'o.order_date') }} as date) as sales_month,
    s.region_code,
    sum(o.order_total_nzd) as total_sales_nzd,
    count(*) as order_count
from {{ ref('fct_orders') }} as o
inner join {{ ref('dim_stores') }} as s on o.region_code = s.region_code
where o.is_revenue_order
group by 1, 2
