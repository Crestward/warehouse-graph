-- Canonical daily revenue reconciles with the order-grain fact (gross) and refunds fact.
with daily as (
    select sum(gross_revenue_nzd) as gross, sum(refunds_nzd) as refunds from {{ ref('fct_revenue_daily') }}
),

orders as (
    select sum(order_total_nzd) as gross from {{ ref('fct_orders') }} where is_revenue_order
),

refunds as (
    select sum(refund_amount_nzd) as refunds from {{ ref('fct_refunds') }}
)

select *
from daily, orders, refunds
where abs(daily.gross - orders.gross) > 0.01 or abs(daily.refunds - refunds.refunds) > 0.01
