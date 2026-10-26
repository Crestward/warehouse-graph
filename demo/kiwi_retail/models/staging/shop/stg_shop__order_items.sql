select
    order_item_id,
    order_id,
    product_id,
    quantity,
    unit_price,
    discount_amount,
    quantity * unit_price - discount_amount as line_amount
from {{ source('shop', 'order_items') }}
