-- Gold: workspace-level product health inputs over the trailing 30 days.
-- One row per workspace, as of snapshot_date (the latest event date loaded).
-- Consumed by analytics/health_scoring.py and lookml/views/product_health.view.lkml.
--
-- Not included because no source data exists yet: NPS, expansion revenue,
-- share of AI-automated calls (agent evaluations are not linked to workspaces).
WITH snapshot AS (
    SELECT MAX(event_date) AS snapshot_date
    FROM {{ ref('stg_events') }}
),
recent_events AS (
    SELECT e.*
    FROM {{ ref('stg_events') }} e
    CROSS JOIN snapshot s
    WHERE e.event_date > s.snapshot_date - 30
),
activity AS (
    SELECT
        workspace_id,
        COUNT(DISTINCT user_id) AS active_users_30d,
        SUM(CASE WHEN event_name = 'support.ticket_created' THEN 1 ELSE 0 END)
            AS support_tickets_last_30d
    FROM recent_events
    GROUP BY workspace_id
),
features AS (
    SELECT
        fu.workspace_id,
        COUNT(DISTINCT fu.feature_name) AS features_adopted_count,
        BOOL_OR(fu.feature_name IN ('ai_assist', 'ai_voice_agent')) AS used_ai_feature_30d
    FROM {{ ref('int_feature_usage') }} fu
    CROSS JOIN snapshot s
    WHERE fu.event_date > s.snapshot_date - 30
    GROUP BY fu.workspace_id
),
sessions AS (
    SELECT
        se.workspace_id,
        AVG(se.session_duration_min) AS avg_session_duration_minutes
    FROM {{ ref('int_sessions') }} se
    CROSS JOIN snapshot s
    WHERE se.session_date > s.snapshot_date - 30
    GROUP BY se.workspace_id
)
SELECT
    w.workspace_id,
    w.workspace_name,
    w.plan_tier,
    w.seat_count,
    s.snapshot_date,
    COALESCE(a.active_users_30d, 0) AS active_users_30d,
    ROUND(COALESCE(a.active_users_30d, 0)::NUMERIC / GREATEST(w.seat_count, 1), 4)
        AS dau_over_seats_ratio,
    COALESCE(f.features_adopted_count, 0) AS features_adopted_count,
    COALESCE(f.used_ai_feature_30d, FALSE) AS used_ai_feature_30d,
    ROUND(COALESCE(ss.avg_session_duration_minutes, 0)::NUMERIC, 2)
        AS avg_session_duration_minutes,
    COALESCE(a.support_tickets_last_30d, 0) AS support_tickets_last_30d
FROM {{ ref('stg_workspaces') }} w
CROSS JOIN snapshot s
LEFT JOIN activity a ON a.workspace_id = w.workspace_id
LEFT JOIN features f ON f.workspace_id = w.workspace_id
LEFT JOIN sessions ss ON ss.workspace_id = w.workspace_id
