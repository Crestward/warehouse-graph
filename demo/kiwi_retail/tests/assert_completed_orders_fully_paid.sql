-- Every completed order is fully paid (successful payments = order total).
select order_id, order_total, paid_amount
from {{ ref('fct_orders') }}
where order_status = 'completed'
  and abs(paid_amount - order_total) > 0.01
