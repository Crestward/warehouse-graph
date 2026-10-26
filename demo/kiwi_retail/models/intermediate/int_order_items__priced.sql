-- Order lines with product attributes, converted to NZD, with line cost and margin.
select
    i.order_item_id,
    i.order_id,
    i.product_id,
    p.category,
    p.product_name,
    i.quantity,
    i.unit_price,
    i.discount_amount,
    i.line_amount,
    o.currency_code,
    {{ to_nzd('i.line_amount', 'o.currency_code') }} as line_amount_nzd,
    round(cast(i.quantity * p.unit_cost_nzd as {{ dbt.type_numeric() }}), 2) as line_cost_nzd
from {{ ref('stg_shop__order_items') }} as i
inner join {{ ref('stg_shop__orders') }} as o on i.order_id = o.order_id
inner join {{ ref('stg_shop__products') }} as p on i.product_id = p.product_id
