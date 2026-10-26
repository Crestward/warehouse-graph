-- Orders with store attributes and the fixed NZD conversion rate.
select
    o.order_id,
    o.customer_id,
    o.store_id,
    s.store_name,
    s.region_code,
    s.channel,
    o.ordered_at,
    o.order_date,
    o.order_status,
    o.currency_code,
    case when o.currency_code = 'AUD' then {{ var('aud_to_nzd') }} else 1 end as fx_rate_to_nzd,
    o.shipping_fee,
    o.discount_code,
    o.has_discount,
    o.order_status in ('completed', 'returned') as is_revenue_order
from {{ ref('stg_shop__orders') }} as o
inner join {{ ref('stg_shop__stores') }} as s on o.store_id = s.store_id
