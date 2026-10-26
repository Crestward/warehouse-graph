select
    p.payment_id,
    p.order_id,
    o.region_code,
    o.channel,
    p.payment_method,
    p.payment_status,
    p.paid_at,
    cast(p.paid_at as date) as paid_date,
    p.amount,
    o.currency_code,
    {{ to_nzd('p.amount', 'o.currency_code') }} as amount_nzd
from {{ ref('stg_shop__payments') }} as p
inner join {{ ref('int_orders__enriched') }} as o on p.order_id = o.order_id
