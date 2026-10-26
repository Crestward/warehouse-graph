with staff as (
    select store_id, sum(case when is_current then 1 else 0 end) as current_staff
    from {{ ref('int_employees__store_staff') }}
    group by store_id
)

select
    s.store_id,
    s.store_name,
    s.city,
    s.region_code,
    s.currency_code,
    s.channel,
    s.opened_on,
    coalesce(staff.current_staff, 0) as current_staff
from {{ ref('stg_shop__stores') }} as s
left join staff on s.store_id = staff.store_id
