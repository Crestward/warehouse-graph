select
    product_id,
    sku,
    product_name,
    category,
    list_price_nzd,
    unit_cost_nzd,
    launched_on,
    is_active
from {{ source('shop', 'products') }}
