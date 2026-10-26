with attributed as (
    select
        campaign_id,
        count(*) as attributed_customers,
        sum(case when has_ordered then 1 else 0 end) as converted_customers
    from {{ ref('int_marketing__attributed_customers') }}
    group by campaign_id
),

attributed_revenue as (
    select a.campaign_id, sum(o.net_revenue_nzd) as attributed_net_revenue_nzd
    from {{ ref('int_marketing__attributed_customers') }} as a
    inner join {{ ref('fct_orders') }} as o on a.customer_id = o.customer_id
    group by a.campaign_id
)

select
    c.campaign_id,
    c.campaign_name,
    c.campaign_channel,
    c.region_code,
    c.started_on,
    c.ended_on,
    c.total_spend_nzd,
    coalesce(att.attributed_customers, 0) as attributed_customers,
    coalesce(att.converted_customers, 0) as converted_customers,
    coalesce(ar.attributed_net_revenue_nzd, 0) as attributed_net_revenue_nzd,
    round(cast(c.total_spend_nzd / nullif(att.converted_customers, 0) as {{ dbt.type_numeric() }}), 2) as cac_nzd,
    round(cast(coalesce(ar.attributed_net_revenue_nzd, 0) / nullif(c.total_spend_nzd, 0) as {{ dbt.type_numeric() }}), 2) as roas
from {{ ref('dim_campaigns') }} as c
left join attributed as att on c.campaign_id = att.campaign_id
left join attributed_revenue as ar on c.campaign_id = ar.campaign_id
