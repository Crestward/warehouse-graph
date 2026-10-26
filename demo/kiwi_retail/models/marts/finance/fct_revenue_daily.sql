-- Canonical revenue. Sales are booked on order_date, refunds on refund_date.
with sales as (
    select
        order_date as revenue_date,
        region_code,
        channel,
        sum(order_total_nzd) as gross_revenue_nzd,
        count(*) as orders
    from {{ ref('fct_orders') }}
    where is_revenue_order
    group by order_date, region_code, channel
),

refunds as (
    select
        refund_date as revenue_date,
        region_code,
        channel,
        sum(refund_amount_nzd) as refunds_nzd
    from {{ ref('fct_refunds') }}
    group by refund_date, region_code, channel
),

day_keys as (
    select revenue_date, region_code, channel from sales
    union
    select revenue_date, region_code, channel from refunds
)

select
    {{ dbt.concat(["cast(k.revenue_date as " ~ dbt.type_string() ~ ")", "'-'", "k.region_code", "'-'", "k.channel"]) }} as revenue_day_id,
    k.revenue_date,
    k.region_code,
    k.channel,
    coalesce(s.gross_revenue_nzd, 0) as gross_revenue_nzd,
    coalesce(r.refunds_nzd, 0) as refunds_nzd,
    coalesce(s.gross_revenue_nzd, 0) - coalesce(r.refunds_nzd, 0) as net_revenue_nzd,
    coalesce(s.orders, 0) as orders
from day_keys as k
left join sales as s
    on k.revenue_date = s.revenue_date and k.region_code = s.region_code and k.channel = s.channel
left join refunds as r
    on k.revenue_date = r.revenue_date and k.region_code = r.region_code and k.channel = r.channel
