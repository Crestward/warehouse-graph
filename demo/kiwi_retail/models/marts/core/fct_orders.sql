select
    o.order_id,
    o.customer_id,
    o.store_id,
    o.region_code,
    o.channel,
    o.ordered_at,
    o.order_date,
    o.order_status,
    o.is_revenue_order,
    o.currency_code,
    o.fx_rate_to_nzd,
    o.discount_code,
    coalesce(t.line_count, 0) as line_count,
    coalesce(t.units, 0) as units,
    coalesce(t.items_subtotal, 0) as items_subtotal,
    o.shipping_fee,
    coalesce(t.items_subtotal, 0) + o.shipping_fee as order_total,
    {{ to_nzd('coalesce(t.items_subtotal, 0) + o.shipping_fee', 'o.currency_code') }} as order_total_nzd,
    coalesce(t.items_cost_nzd, 0) as items_cost_nzd,
    coalesce(p.paid_amount, 0) as paid_amount,
    coalesce(p.failed_attempts, 0) as failed_payment_attempts,
    coalesce(p.is_split_tender, false) as is_split_tender,
    coalesce(r.refund_amount, 0) as refund_amount,
    {{ to_nzd('coalesce(r.refund_amount, 0)', 'o.currency_code') }} as refund_amount_nzd,
    case
        when o.is_revenue_order
            then {{ to_nzd('coalesce(t.items_subtotal, 0) + o.shipping_fee - coalesce(r.refund_amount, 0)', 'o.currency_code') }}
        else 0
    end as net_revenue_nzd
from {{ ref('int_orders__enriched') }} as o
left join {{ ref('int_orders__item_totals') }} as t on o.order_id = t.order_id
left join {{ ref('int_payments__by_order') }} as p on o.order_id = p.order_id
left join {{ ref('int_refunds__by_order') }} as r on o.order_id = r.order_id
