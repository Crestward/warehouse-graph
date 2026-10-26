select
    campaign_id,
    campaign_name,
    channel as campaign_channel,
    region_code,
    started_on,
    ended_on,
    budget,
    currency_code
from {{ source('marketing', 'campaigns') }}
