-- Employees with their store and tenure (as of the as_of_date var).
select
    e.employee_id,
    e.first_name,
    e.last_name,
    e.work_email,
    e.job_title,
    e.store_id,
    s.store_name,
    s.region_code,
    e.hired_on,
    e.terminated_on,
    e.terminated_on is null as is_current,
    {{ dbt.datediff('e.hired_on', "coalesce(e.terminated_on, cast('" ~ var('as_of_date') ~ "' as date))", 'day') }} as tenure_days,
    e.annual_salary,
    e.salary_currency,
    e.manager_id
from {{ ref('stg_hr__employees') }} as e
inner join {{ ref('stg_shop__stores') }} as s on e.store_id = s.store_id
