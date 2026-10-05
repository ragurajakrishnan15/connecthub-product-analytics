-- Gold (serving): monthly active users and workspaces, overall and for features.
--
--   active_users / active_workspaces            any event in the month (incl. account events)
--   feature_active_users / _workspaces          any mapped product feature (int_feature_usage)
--   ai_active_users / _workspaces               ai_assist or ai_voice_agent
-- AI feature adoption (semantic metric ai_feature_adoption) =
--   ai_active_workspaces / feature_active_workspaces: the denominator is
--   workspaces using any mapped feature, as in the semantic layer, not every
--   workspace with any event.
--
-- Incremental like fct_feature_usage_monthly: months overlapping the lookback
-- window are recomputed in full.
{{ config(
    materialized='incremental',
    unique_key='month_start',
    incremental_strategy='delete+insert',
    on_schema_change='fail'
) }}
WITH data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
),
events AS (
    SELECT
        DATE_TRUNC('month', event_date)::DATE AS month_start,
        COUNT(DISTINCT user_id) AS active_users,
        COUNT(DISTINCT workspace_id) AS active_workspaces
    FROM {{ ref('stg_events') }}
    {% if is_incremental() %}
    WHERE event_date >= DATE_TRUNC('month', {{ incremental_window_start('month_start') }})::DATE
    {% endif %}
    GROUP BY 1
),
features AS (
    SELECT
        DATE_TRUNC('month', event_date)::DATE AS month_start,
        COUNT(DISTINCT user_id) AS feature_active_users,
        COUNT(DISTINCT workspace_id) AS feature_active_workspaces,
        COUNT(DISTINCT user_id) FILTER (WHERE feature_name IN ('ai_assist', 'ai_voice_agent'))
            AS ai_active_users,
        COUNT(DISTINCT workspace_id) FILTER (WHERE feature_name IN ('ai_assist', 'ai_voice_agent'))
            AS ai_active_workspaces
    FROM {{ ref('int_feature_usage') }}
    {% if is_incremental() %}
    WHERE event_date >= DATE_TRUNC('month', {{ incremental_window_start('month_start') }})::DATE
    {% endif %}
    GROUP BY 1
)
SELECT
    e.month_start,
    e.active_users::BIGINT AS active_users,
    e.active_workspaces::BIGINT AS active_workspaces,
    COALESCE(f.feature_active_users, 0)::BIGINT AS feature_active_users,
    COALESCE(f.feature_active_workspaces, 0)::BIGINT AS feature_active_workspaces,
    COALESCE(f.ai_active_users, 0)::BIGINT AS ai_active_users,
    COALESCE(f.ai_active_workspaces, 0)::BIGINT AS ai_active_workspaces,
    ((e.month_start + INTERVAL '1 month' - INTERVAL '1 day')::DATE <= d.last_event_date)
        AS month_complete
FROM events e
LEFT JOIN features f ON f.month_start = e.month_start
CROSS JOIN data_end d
