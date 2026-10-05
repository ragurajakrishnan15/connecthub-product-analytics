-- Staging: user dimension with signup date, plan, workspace
SELECT
    user_id,
    workspace_id,
    signup_date::DATE AS signup_date,
    first_active_date::DATE AS first_active_date,
    plan_tier,
    country_code,
    is_admin
FROM {{ source('bronze', 'users_raw') }}
WHERE user_id IS NOT NULL
  AND signup_date IS NOT NULL
