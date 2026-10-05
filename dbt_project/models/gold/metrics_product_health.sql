-- Gold: workspace-level product health inputs, one row per workspace as of
-- snapshot_date (the latest event date loaded). Activity, feature, session,
-- support and AI-automation inputs cover the trailing 30 days; NPS the trailing
-- 90 days; revenue is the snapshot month's MRR and its change from the month before.
-- Consumed by analytics/health_scoring.py and lookml/views/product_health.view.lkml.
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
),
calls AS (
    -- Share of all calls (human-placed + AI-handled) that the AI agent resolved alone
    SELECT
        workspace_id,
        SUM(ai_resolved) AS ai_resolved_calls_30d,
        SUM(total_calls) AS total_calls_30d
    FROM (
        SELECT e.workspace_id, 0 AS ai_resolved, 1 AS total_calls
        FROM recent_events e
        WHERE e.event_name = 'call.started'
        UNION ALL
        SELECT ev.workspace_id,
               CASE WHEN ev.resolution_path = 'ai_resolved' THEN 1 ELSE 0 END,
               1
        FROM {{ ref('fct_agent_evaluations') }} ev
        CROSS JOIN snapshot s
        WHERE ev.call_date > s.snapshot_date - 30
    ) c
    GROUP BY workspace_id
),
nps AS (
    SELECT
        n.workspace_id,
        COUNT(*) AS nps_responses_90d,
        ROUND(100.0 * (COUNT(*) FILTER (WHERE n.nps_category = 'promoter')
                       - COUNT(*) FILTER (WHERE n.nps_category = 'detractor')) / COUNT(*), 1)
            AS nps_score
    FROM {{ ref('stg_nps_responses') }} n
    CROSS JOIN snapshot s
    WHERE n.response_date > s.snapshot_date - 90
    GROUP BY n.workspace_id
),
revenue AS (
    SELECT m.workspace_id, m.mrr_usd, m.mrr_change_usd
    FROM {{ ref('fct_workspace_mrr') }} m
    CROSS JOIN snapshot s
    WHERE m.month_start = DATE_TRUNC('month', s.snapshot_date)::DATE
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
    COALESCE(a.support_tickets_last_30d, 0) AS support_tickets_last_30d,
    ROUND(COALESCE(c.ai_resolved_calls_30d, 0)::NUMERIC / NULLIF(c.total_calls_30d, 0), 4)
        AS pct_ai_calls_automated,
    n.nps_score,  -- NULL when the workspace has no responses in the window
    COALESCE(n.nps_responses_90d, 0) AS nps_responses_90d,
    COALESCE(r.mrr_usd, 0) AS mrr_usd,
    COALESCE(r.mrr_change_usd, 0) AS mrr_change_usd
FROM {{ ref('stg_workspaces') }} w
CROSS JOIN snapshot s
LEFT JOIN activity a ON a.workspace_id = w.workspace_id
LEFT JOIN features f ON f.workspace_id = w.workspace_id
LEFT JOIN sessions ss ON ss.workspace_id = w.workspace_id
LEFT JOIN calls c ON c.workspace_id = w.workspace_id
LEFT JOIN nps n ON n.workspace_id = w.workspace_id
LEFT JOIN revenue r ON r.workspace_id = w.workspace_id
