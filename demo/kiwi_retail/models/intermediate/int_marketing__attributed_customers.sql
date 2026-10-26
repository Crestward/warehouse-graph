-- Attributed customers with their campaign and whether/when they first ordered.
select
    a.customer_id,
    a.campaign_id,
    c.campaign_channel,
    c.region_code,
    a.attributed_on,
    h.first_order_date,
    h.first_order_date is not null as has_ordered
from {{ ref('stg_marketing__attributions') }} as a
inner join {{ ref('stg_marketing__campaigns') }} as c on a.campaign_id = c.campaign_id
left join {{ ref('int_customers__order_history') }} as h on a.customer_id = h.customer_id
