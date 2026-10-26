select
    store_id,
    store_name,
    city,
    region_code,
    currency_code,
    channel,
    opened_on
from {{ source('shop', 'stores') }}
