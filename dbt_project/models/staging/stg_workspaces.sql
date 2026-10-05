-- Staging: workspace dimension with creation date, plan tier, seat count
SELECT
    workspace_id,
    workspace_name,
    created_date::DATE AS created_date,
    plan_tier,
    seat_count,
    country_code
FROM {{ source('bronze', 'workspaces_raw') }}
WHERE workspace_id IS NOT NULL
