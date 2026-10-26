select
    refund_id,
    order_id,
    refunded_at,
    cast(refunded_at as date) as refund_date,
    amount as refund_amount,
    reason as refund_reason
from {{ source('shop', 'refunds') }}
