select
    employee_id,
    first_name,
    last_name,
    work_email,
    store_id,
    job_title,
    hired_on,
    terminated_on,
    annual_salary,
    salary_currency,
    manager_id
from {{ source('hr', 'employees') }}
