select
    customer_id,
    campaign_id,
    attributed_on,
    model as attribution_model
from {{ source('marketing', 'attributions') }}
