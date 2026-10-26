-- Daily spend per campaign with campaign attributes, converted to NZD.
select
    a.ad_spend_id,
    a.campaign_id,
    c.campaign_name,
    c.campaign_channel,
    c.region_code,
    a.spend_date,
    a.spend,
    c.currency_code,
    {{ to_nzd('a.spend', 'c.currency_code') }} as spend_nzd,
    a.impressions,
    a.clicks
from {{ ref('stg_marketing__ad_spend') }} as a
inner join {{ ref('stg_marketing__campaigns') }} as c on a.campaign_id = c.campaign_id
