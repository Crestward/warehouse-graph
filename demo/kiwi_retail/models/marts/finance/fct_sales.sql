-- Planted trap (misleading name): looks like the canonical sales fact, but it is line-level
-- GROSS sales in LOCAL currency (NZD and AUD mixed), including cancelled and pending orders,
-- with no shipping and no refunds. Use fct_revenue_daily / net_revenue for revenue.
select
    i.order_item_id,
    i.order_id,
    o.store_id,
    o.region_code,
    o.order_date as sold_on,
    o.order_status,
    i.product_id,
    i.category,
    i.quantity,
    i.line_amount as sales_amount,
    o.currency_code
from {{ ref('int_order_items__priced') }} as i
inner join {{ ref('int_orders__enriched') }} as o on i.order_id = o.order_id
