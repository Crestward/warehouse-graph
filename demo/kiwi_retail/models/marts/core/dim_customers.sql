select
    c.customer_id,
    c.first_name,
    c.last_name,
    c.email,
    c.phone,
    c.city,
    c.region_code,
    c.signed_up_on,
    c.marketing_opt_in,
    case
        when coalesce(h.revenue_orders, 0) >= 8 or coalesce(h.lifetime_gross_nzd, 0) >= 1500 then 'VIP'
        when coalesce(h.revenue_orders, 0) >= 2 then 'Repeat'
        when coalesce(h.revenue_orders, 0) = 1 then 'One-off'
        else 'Prospect'
    end as customer_segment,
    c.legacy_segment,
    h.first_order_date,
    h.last_order_date,
    coalesce(h.revenue_orders, 0) as lifetime_orders,
    coalesce(h.lifetime_gross_nzd, 0) as lifetime_gross_nzd,
    coalesce(a.campaign_channel, 'organic') as acquisition_channel
from {{ ref('stg_shop__customers') }} as c
left join {{ ref('int_customers__order_history') }} as h on c.customer_id = h.customer_id
left join {{ ref('int_marketing__attributed_customers') }} as a on c.customer_id = a.customer_id
