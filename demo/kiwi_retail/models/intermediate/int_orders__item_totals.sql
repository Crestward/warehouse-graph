-- One row per order: line counts and item totals (before shipping).
select
    order_id,
    count(*) as line_count,
    sum(quantity) as units,
    sum(line_amount) as items_subtotal,
    sum(line_amount_nzd) as items_subtotal_nzd,
    sum(line_cost_nzd) as items_cost_nzd
from {{ ref('int_order_items__priced') }}
group by order_id
