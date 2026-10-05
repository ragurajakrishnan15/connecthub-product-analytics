-- Gold (serving): histogram of days from signup to each activation milestone,
-- per signup date and billed plan. Only users who reached the milestone within
-- 14 days are counted, so days_to_milestone is 0..14 and medians computed from
-- this table are conditional on reaching the milestone. Integer days make the
-- median exact (no interpolation).
--   activated_14d: the day the last of the three milestones was reached.
WITH data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
),
reached AS (
    SELECT signup_date, plan_tier, 'placed_first_call' AS milestone,
           first_call_date - signup_date AS days_to_milestone
    FROM {{ ref('int_user_signup_plan') }} WHERE placed_first_call = 1
    UNION ALL
    SELECT signup_date, plan_tier, 'used_ai_feature', first_ai_date - signup_date
    FROM {{ ref('int_user_signup_plan') }} WHERE used_ai_feature = 1
    UNION ALL
    SELECT signup_date, plan_tier, 'invited_team_member', first_invite_date - signup_date
    FROM {{ ref('int_user_signup_plan') }} WHERE invited_team_member = 1
    UNION ALL
    SELECT signup_date, plan_tier, 'activated_14d',
           GREATEST(first_call_date, first_ai_date, first_invite_date) - signup_date
    FROM {{ ref('int_user_signup_plan') }}
    WHERE placed_first_call = 1 AND used_ai_feature = 1 AND invited_team_member = 1
)
SELECT
    r.signup_date,
    r.plan_tier,
    r.milestone,
    r.days_to_milestone,
    COUNT(*)::BIGINT AS users,
    (r.signup_date + 14 <= d.last_event_date) AS window_14d_complete
FROM reached r
CROSS JOIN data_end d
GROUP BY r.signup_date, r.plan_tier, r.milestone, r.days_to_milestone, d.last_event_date
