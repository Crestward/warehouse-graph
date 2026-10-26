select
    r.refund_id,
    r.order_id,
    o.customer_id,
    o.region_code,
    o.channel,
    o.order_date,
    r.refunded_at,
    r.refund_date,
    r.refund_reason,
    r.refund_amount,
    o.currency_code,
    {{ to_nzd('r.refund_amount', 'o.currency_code') }} as refund_amount_nzd
from {{ ref('stg_shop__refunds') }} as r
inner join {{ ref('int_orders__enriched') }} as o on r.order_id = o.order_id
