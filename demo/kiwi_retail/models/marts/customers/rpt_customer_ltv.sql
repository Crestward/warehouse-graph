select
    c.customer_id,
    c.region_code,
    c.customer_segment,
    c.first_order_date,
    count(o.order_id) as revenue_orders,
    coalesce(sum(o.order_total_nzd), 0) as gross_ltv_nzd,
    coalesce(sum(o.refund_amount_nzd), 0) as refunds_nzd,
    coalesce(sum(o.net_revenue_nzd), 0) as net_ltv_nzd,
    round(cast(sum(o.order_total_nzd) / nullif(count(o.order_id), 0) as {{ dbt.type_numeric() }}), 2) as avg_order_value_nzd
from {{ ref('dim_customers') }} as c
left join {{ ref('fct_orders') }} as o
    on c.customer_id = o.customer_id and o.is_revenue_order
group by c.customer_id, c.region_code, c.customer_segment, c.first_order_date
