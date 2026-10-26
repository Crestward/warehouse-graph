select
    payment_id,
    order_id,
    payment_method,
    amount,
    status as payment_status,
    created_at as paid_at
from {{ source('shop', 'payments') }}
