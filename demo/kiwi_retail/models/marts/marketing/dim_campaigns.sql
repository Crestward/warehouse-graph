with spend as (
    select campaign_id, sum(spend_nzd) as total_spend_nzd, count(*) as active_days
    from {{ ref('int_marketing__spend_daily') }}
    group by campaign_id
)

select
    c.campaign_id,
    c.campaign_name,
    c.campaign_channel,
    c.region_code,
    c.started_on,
    c.ended_on,
    c.budget,
    c.currency_code,
    {{ to_nzd('c.budget', 'c.currency_code') }} as budget_nzd,
    coalesce(spend.total_spend_nzd, 0) as total_spend_nzd,
    coalesce(spend.active_days, 0) as active_days
from {{ ref('stg_marketing__campaigns') }} as c
left join spend on c.campaign_id = spend.campaign_id
