select
    order_id,
    customer_id,
    store_id,
    ordered_at,
    cast(ordered_at as date) as order_date,
    status as order_status,
    currency_code,
    shipping_fee,
    discount_code,
    discount_code is not null as has_discount
from {{ source('shop', 'orders') }}
