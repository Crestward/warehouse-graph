-- One row per order that has any payment attempt. Only successful payments are summed.
select
    order_id,
    sum(case when payment_status = 'success' then amount else 0 end) as paid_amount,
    sum(case when payment_status = 'success' then 1 else 0 end) as successful_payments,
    sum(case when payment_status = 'failed' then 1 else 0 end) as failed_attempts,
    count(distinct case when payment_status = 'success' then payment_method end) > 1 as is_split_tender,
    max(case when payment_status = 'success' then paid_at end) as last_paid_at
from {{ ref('stg_shop__payments') }}
group by order_id
