{%- set days_since = dbt.datediff('last_order_date', "cast('" ~ var('as_of_date') ~ "' as date)", 'day') -%}
select
    customer_id,
    region_code,
    signed_up_on,
    customer_segment,
    last_order_date,
    {{ days_since }} as days_since_last_order,
    -- canonical: customers who have ordered, with no revenue order in the last 90 days
    coalesce({{ days_since }} > 90, false) as is_churned,
    -- marketing's legacy definition: 180 days, and never-ordered customers count as churned
    coalesce({{ days_since }} > 180, true) as is_churned_marketing,
    last_order_date is not null as has_ordered
from {{ ref('dim_customers') }}
