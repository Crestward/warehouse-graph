select
    {{ dbt.concat(["cast(campaign_id as " ~ dbt.type_string() ~ ")", "'-'", "cast(spend_date as " ~ dbt.type_string() ~ ")"]) }} as ad_spend_id,
    campaign_id,
    spend_date,
    spend,
    impressions,
    clicks
from {{ source('marketing', 'ad_spend') }}
