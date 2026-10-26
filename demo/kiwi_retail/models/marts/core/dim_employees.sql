select
    employee_id,
    first_name,
    last_name,
    work_email,
    job_title,
    store_id,
    store_name,
    region_code,
    hired_on,
    terminated_on,
    is_current,
    tenure_days,
    annual_salary,
    salary_currency,
    manager_id
from {{ ref('int_employees__store_staff') }}
