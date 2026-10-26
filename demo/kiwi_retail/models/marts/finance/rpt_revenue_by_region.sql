select
    cast({{ dbt.date_trunc('month', 'revenue_date') }} as date) as revenue_month,
    region_code,
    sum(gross_revenue_nzd) as gross_revenue_nzd,
    sum(refunds_nzd) as refunds_nzd,
    sum(net_revenue_nzd) as net_revenue_nzd,
    sum(orders) as orders,
    round(cast(sum(gross_revenue_nzd) / nullif(sum(orders), 0) as {{ dbt.type_numeric() }}), 2) as avg_order_value_nzd
from {{ ref('fct_revenue_daily') }}
group by 1, 2
