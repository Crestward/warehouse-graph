select
    ad_spend_id,
    campaign_id,
    campaign_channel,
    region_code,
    spend_date,
    spend_nzd,
    impressions,
    clicks
from {{ ref('int_marketing__spend_daily') }}
