-- Planted trap (misleading name): contains ALL customers, active or not. Filter on is_active.
select
    customer_id,
    region_code,
    customer_segment,
    last_order_date,
    coalesce({{ dbt.datediff('last_order_date', "cast('" ~ var('as_of_date') ~ "' as date)", 'day') }} <= 180, false) as is_active
from {{ ref('dim_customers') }}
