-- Gold (serving): support-ticket volume and active users per day and billed plan.
--
--   tickets_created / tickets_resolved   counts of support.ticket_created / _resolved events
--   active_users                         distinct users with any event that day; summed
--                                        over days it is *active user-days* (the
--                                        denominator for tickets per 1,000 active user-days)
-- Events carry no ticket ID, so a resolution cannot be matched to its ticket:
-- resolution time, backlog and per-ticket resolution rates are NOT derivable.
--
-- Incremental: whole dates in the lookback window (macros/incremental_window.sql)
-- are deleted and recomputed (unique_key = event_date, not the grain), so a plan
-- that has no events on a reprocessed date cannot leave a stale row behind.
{{ config(
    materialized='incremental',
    unique_key='event_date',
    incremental_strategy='delete+insert',
    on_schema_change='fail'
) }}
WITH events AS (
    SELECT
        e.event_date,
        e.user_id,
        e.event_name,
        COALESCE(s.plan_tier, w.plan_tier) AS plan_tier
    FROM {{ ref('stg_events') }} e
    LEFT JOIN {{ ref('stg_workspaces') }} w ON w.workspace_id = e.workspace_id
    LEFT JOIN {{ ref('stg_subscriptions') }} s
        ON s.workspace_id = e.workspace_id
       AND s.month_start = DATE_TRUNC('month', e.event_date)::DATE
    {% if is_incremental() %}
    WHERE e.event_date >= {{ incremental_window_start('event_date') }}
    {% endif %}
)
SELECT
    event_date,
    plan_tier,
    COUNT(DISTINCT user_id)::BIGINT AS active_users,
    COUNT(*) FILTER (WHERE event_name = 'support.ticket_created')::BIGINT AS tickets_created,
    COUNT(*) FILTER (WHERE event_name = 'support.ticket_resolved')::BIGINT AS tickets_resolved
FROM events
GROUP BY event_date, plan_tier
