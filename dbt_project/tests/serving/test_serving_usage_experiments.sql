-- Monthly usage, experiment curve and censoring-aware feature adoption.
-- Returns one row per violation.
{{ config(tags=['serving']) }}
WITH data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
)
-- AI-active <= feature-active <= active, for users and workspaces
SELECT 'activity_order' AS failure, month_start::TEXT AS detail
FROM {{ ref('fct_activity_monthly') }}
WHERE NOT (ai_active_users <= feature_active_users AND feature_active_users <= active_users
           AND ai_active_workspaces <= feature_active_workspaces
           AND feature_active_workspaces <= active_workspaces AND active_users > 0)
UNION ALL
-- a feature's monthly users/workspaces never exceed the feature-active totals
SELECT 'feature_vs_activity', f.month_start::TEXT || ' ' || f.feature_name
FROM {{ ref('fct_feature_usage_monthly') }} f
JOIN {{ ref('fct_activity_monthly') }} a ON a.month_start = f.month_start
WHERE f.active_users > a.feature_active_users
   OR f.active_workspaces > a.feature_active_workspaces
   OR f.active_users <= 0 OR f.usage_events < f.active_users
UNION ALL
-- month_complete is exactly "month end <= last event date"
SELECT 'month_complete_flag', m.month_start::TEXT
FROM {{ ref('fct_activity_monthly') }} m CROSS JOIN data_end e
WHERE m.month_complete <> ((m.month_start + INTERVAL '1 month' - INTERVAL '1 day')::DATE
                           <= e.last_event_date)
UNION ALL
-- cumulative activation never decreases, never exceeds the arm, and day 14
-- equals the arm's activated users with a complete window
SELECT 'curve_monotonic', experiment_id || ' ' || variant || ' ' || day_since_signup::TEXT
FROM (
    SELECT *, LAG(activated_cumulative) OVER (
        PARTITION BY experiment_id, variant ORDER BY day_since_signup) AS prev
    FROM {{ ref('fct_experiment_activation_curve') }}) c
WHERE activated_cumulative < COALESCE(prev, 0) OR activated_cumulative > users_in_window
   OR cumulative_activation_rate NOT BETWEEN 0 AND 1
UNION ALL
SELECT 'curve_day14', c.experiment_id || ' ' || c.variant
FROM {{ ref('fct_experiment_activation_curve') }} c
JOIN (SELECT experiment_id, variant, COUNT(*) AS n, SUM(activated_14d) AS activated
      FROM {{ ref('fct_experiment_user_metrics') }}
      WHERE window_14d_complete GROUP BY experiment_id, variant) m
  ON m.experiment_id = c.experiment_id AND m.variant = c.variant
WHERE c.day_since_signup = 14
  AND (c.activated_cumulative <> m.activated OR c.users_in_window <> m.n)
UNION ALL
-- censoring-aware adoption: eligible users shrink with D, adopters within them
SELECT 'adoption_observed', feature_name || ' ' || days_since_signup::TEXT
FROM (
    SELECT *, LAG(eligible_users) OVER (
        PARTITION BY feature_name ORDER BY days_since_signup) AS prev_eligible
    FROM {{ ref('fct_feature_adoption') }}) a
WHERE eligible_adopters > eligible_users OR eligible_users > total_users
   OR eligible_users > COALESCE(prev_eligible, eligible_users)
   OR (eligible_users = 0 AND observed_adoption_pct IS NOT NULL)
   OR observed_adoption_pct NOT BETWEEN 0 AND 100
UNION ALL
-- day 0: every user who signed up by the last event date is observable
SELECT 'adoption_day0_eligible', feature_name
FROM {{ ref('fct_feature_adoption') }} a CROSS JOIN data_end e
WHERE a.days_since_signup = 0
  AND a.eligible_users <> (SELECT COUNT(*) FROM {{ ref('stg_users') }}
                           WHERE signup_date <= e.last_event_date)
