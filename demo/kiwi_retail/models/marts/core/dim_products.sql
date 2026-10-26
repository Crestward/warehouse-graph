select
    product_id,
    sku,
    product_name,
    category,
    list_price_nzd,
    unit_cost_nzd,
    round(cast((list_price_nzd - unit_cost_nzd) / nullif(list_price_nzd, 0) as {{ dbt.type_numeric() }}), 4) as list_margin_pct,
    launched_on,
    is_active
from {{ ref('stg_shop__products') }}
