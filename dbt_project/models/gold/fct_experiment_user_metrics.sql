-- Gold: one row per experiment participant with the metrics experiments are judged on.
-- "Within N days" means event_date <= signup_date + N, as in int_activation_funnel.
--   activated_14d            primary: placed a call, used an AI feature and invited a teammate
--   avg_session_minutes_14d  guardrail: mean session length (NULL if no sessions)
--   revenue_60d              guardrail: seat revenue billed for the user, i.e. the seat price of
--                            each month in which the user was active within 60 days
-- window_*_complete is FALSE when the data ends before the user's window does; evaluations
-- should only use users with a complete window for each metric.
WITH funnel AS (
    SELECT user_id, signup_date, placed_first_call, used_ai_feature, invited_team_member
    FROM {{ ref('int_activation_funnel') }}
),
data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
),
sessions_14d AS (
    SELECT
        s.user_id,
        COUNT(*) AS sessions_14d,
        AVG(s.session_duration_min) AS avg_session_minutes_14d
    FROM {{ ref('int_sessions') }} s
    JOIN funnel f ON f.user_id = s.user_id
    WHERE s.session_date <= f.signup_date + 14
    GROUP BY s.user_id
),
active_months AS (
    SELECT DISTINCT
        e.user_id,
        e.workspace_id,
        DATE_TRUNC('month', e.event_date)::DATE AS month_start
    FROM {{ ref('stg_events') }} e
    JOIN funnel f ON f.user_id = e.user_id
    WHERE e.event_date <= f.signup_date + 60
),
revenue AS (
    SELECT am.user_id, SUM(sub.seat_price_usd) AS revenue_60d
    FROM active_months am
    JOIN {{ ref('stg_subscriptions') }} sub
        ON sub.workspace_id = am.workspace_id
       AND sub.month_start = am.month_start
    GROUP BY am.user_id
)
SELECT
    a.experiment_id,
    a.variant,
    a.user_id,
    f.signup_date,
    CASE WHEN f.placed_first_call = 1 AND f.used_ai_feature = 1 AND f.invited_team_member = 1
         THEN 1 ELSE 0 END AS activated_14d,
    COALESCE(s.sessions_14d, 0) AS sessions_14d,
    ROUND(s.avg_session_minutes_14d::NUMERIC, 4) AS avg_session_minutes_14d,
    COALESCE(r.revenue_60d, 0) AS revenue_60d,
    f.signup_date + 14 <= d.last_event_date AS window_14d_complete,
    f.signup_date + 60 <= d.last_event_date AS window_60d_complete
FROM {{ ref('fct_experiment_assignments') }} a
JOIN funnel f ON f.user_id = a.user_id
CROSS JOIN data_end d
LEFT JOIN sessions_14d s ON s.user_id = a.user_id
LEFT JOIN revenue r ON r.user_id = a.user_id
