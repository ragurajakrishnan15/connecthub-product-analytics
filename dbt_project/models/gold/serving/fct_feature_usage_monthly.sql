-- Gold (serving): monthly active users and workspaces per feature.
-- Features come from int_feature_usage (event -> feature map in analytics/features.py).
-- Distinct counts are not additive across months; query one month per row.
--
-- Incremental: every month that overlaps the lookback window is recomputed in
-- full from int_feature_usage (unique_key = month_start), because a distinct
-- count for a month needs all of that month's rows.
{{ config(
    materialized='incremental',
    unique_key='month_start',
    incremental_strategy='delete+insert',
    on_schema_change='fail'
) }}
WITH data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
),
usage AS (
    SELECT
        DATE_TRUNC('month', event_date)::DATE AS month_start,
        feature_name,
        user_id,
        workspace_id,
        usage_count
    FROM {{ ref('int_feature_usage') }}
    {% if is_incremental() %}
    WHERE event_date >= DATE_TRUNC('month', {{ incremental_window_start('month_start') }})::DATE
    {% endif %}
)
SELECT
    u.month_start,
    u.feature_name,
    COUNT(DISTINCT u.workspace_id)::BIGINT AS active_workspaces,
    COUNT(DISTINCT u.user_id)::BIGINT AS active_users,
    SUM(u.usage_count)::BIGINT AS usage_events,
    ((u.month_start + INTERVAL '1 month' - INTERVAL '1 day')::DATE <= d.last_event_date)
        AS month_complete
FROM usage u
CROSS JOIN data_end d
GROUP BY u.month_start, u.feature_name, d.last_event_date
