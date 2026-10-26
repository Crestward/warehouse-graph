select
    customer_id,
    first_name,
    last_name,
    lower(email) as email,
    phone,
    city,
    region_code,
    signed_up_on,
    marketing_opt_in,
    legacy_segment
from {{ source('shop', 'customers') }}
