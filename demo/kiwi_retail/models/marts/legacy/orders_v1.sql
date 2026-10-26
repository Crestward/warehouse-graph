-- DEPRECATED (planted): the 2022 orders model. Sums every payment attempt (including failed
-- ones), ignores currency and refunds. Replaced by fct_orders.
select
    o.order_id,
    o.customer_id,
    o.store_id,
    o.order_date,
    o.order_status,
    sum(p.amount) as order_amount
from {{ ref('stg_shop__orders') }} as o
left join {{ ref('stg_shop__payments') }} as p on o.order_id = p.order_id
group by o.order_id, o.customer_id, o.store_id, o.order_date, o.order_status
