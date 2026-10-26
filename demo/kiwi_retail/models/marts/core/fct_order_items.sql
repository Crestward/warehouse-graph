select
    i.order_item_id,
    i.order_id,
    o.customer_id,
    o.store_id,
    o.region_code,
    o.channel,
    o.order_date,
    o.order_status,
    o.is_revenue_order,
    i.product_id,
    i.product_name,
    i.category,
    i.quantity,
    i.unit_price,
    i.discount_amount,
    i.line_amount,
    i.currency_code,
    i.line_amount_nzd,
    i.line_cost_nzd,
    i.line_amount_nzd - i.line_cost_nzd as line_margin_nzd
from {{ ref('int_order_items__priced') }} as i
inner join {{ ref('int_orders__enriched') }} as o on i.order_id = o.order_id
