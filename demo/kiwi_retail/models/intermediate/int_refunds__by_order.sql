-- One row per refunded order.
select
    order_id,
    count(*) as refund_count,
    sum(refund_amount) as refund_amount,
    min(refund_date) as first_refund_date
from {{ ref('stg_shop__refunds') }}
group by order_id
