-- One row per customer with at least one revenue order (completed or returned).
select
    o.customer_id,
    min(o.order_date) as first_order_date,
    max(o.order_date) as last_order_date,
    count(*) as revenue_orders,
    sum(t.items_subtotal_nzd + {{ to_nzd('o.shipping_fee', 'o.currency_code') }}) as lifetime_gross_nzd
from {{ ref('int_orders__enriched') }} as o
inner join {{ ref('int_orders__item_totals') }} as t on o.order_id = t.order_id
where o.is_revenue_order
group by o.customer_id
