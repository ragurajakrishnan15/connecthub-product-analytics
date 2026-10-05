-- Activation serving models: funnel ordering, totals, window flag and milestone
-- histogram reconciliation. Returns one row per violation.
{{ config(tags=['serving']) }}
WITH data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
),
daily AS (
    SELECT * FROM {{ ref('fct_activation_daily') }}
),
milestones AS (
    SELECT milestone, SUM(users) AS users
    FROM {{ ref('fct_activation_milestone_days') }}
    GROUP BY milestone
),
funnel_totals AS (
    SELECT 'placed_first_call' AS milestone, SUM(placed_first_call) AS users FROM daily
    UNION ALL SELECT 'used_ai_feature', SUM(used_ai_feature) FROM daily
    UNION ALL SELECT 'invited_team_member', SUM(invited_team_member) FROM daily
    UNION ALL SELECT 'activated_14d', SUM(activated_14d) FROM daily
)
-- strict funnel order and bounds within every row
SELECT 'funnel_order' AS failure, signup_date::TEXT AS detail
FROM daily
WHERE NOT (activated_14d <= call_and_ai AND call_and_ai <= placed_first_call
           AND placed_first_call <= signups AND call_and_ai <= used_ai_feature
           AND activated_14d <= invited_team_member
           AND used_ai_feature <= signups AND invited_team_member <= signups
           AND activated_14d >= 0 AND signups > 0)
UNION ALL
-- every user is counted exactly once
SELECT 'signup_total', (SELECT SUM(signups) FROM daily)::TEXT
WHERE (SELECT SUM(signups) FROM daily) <> (SELECT COUNT(*) FROM {{ ref('int_activation_funnel') }})
UNION ALL
-- activated users match the per-user funnel
SELECT 'activated_total', (SELECT SUM(activated_14d) FROM daily)::TEXT
WHERE (SELECT SUM(activated_14d) FROM daily) <> (
    SELECT COUNT(*) FROM {{ ref('int_activation_funnel') }}
    WHERE placed_first_call = 1 AND used_ai_feature = 1 AND invited_team_member = 1)
UNION ALL
-- window flag is exactly "signup_date + 14 <= last event date" (boundary inclusive)
SELECT 'window_flag', d.signup_date::TEXT
FROM daily d CROSS JOIN data_end e
WHERE d.window_14d_complete <> (d.signup_date + 14 <= e.last_event_date)
UNION ALL
-- the histogram holds exactly the users who reached each milestone
SELECT 'milestone_total_' || f.milestone, COALESCE(m.users, 0)::TEXT
FROM funnel_totals f LEFT JOIN milestones m USING (milestone)
WHERE COALESCE(m.users, 0) <> f.users
UNION ALL
-- days to a milestone are within the 14-day window
SELECT 'milestone_days_range', days_to_milestone::TEXT
FROM {{ ref('fct_activation_milestone_days') }}
WHERE days_to_milestone NOT BETWEEN 0 AND 14 OR users <= 0
