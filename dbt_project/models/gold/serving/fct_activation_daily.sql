-- Gold (serving): 14-day activation counts per signup date and billed plan.
--
-- Additive counts, so any signup-date range re-aggregates exactly:
--   placed_first_call / used_ai_feature / invited_team_member
--       independent milestone flags from int_activation_funnel (event within 14 days)
--   call_and_ai      placed a call AND used an AI feature (strict funnel step 3)
--   activated_14d    all three milestones (strict funnel step 4 = the activation definition)
-- window_14d_complete is FALSE for signup dates whose 14-day window extends past
-- the last loaded event date: those users may still activate, so rates should
-- be computed over complete windows only (docs/metric-definitions.md).
WITH data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
)
SELECT
    u.signup_date,
    u.plan_tier,
    COUNT(*)::BIGINT AS signups,
    SUM(u.placed_first_call)::BIGINT AS placed_first_call,
    SUM(u.used_ai_feature)::BIGINT AS used_ai_feature,
    SUM(u.invited_team_member)::BIGINT AS invited_team_member,
    SUM(u.placed_first_call * u.used_ai_feature)::BIGINT AS call_and_ai,
    SUM(u.placed_first_call * u.used_ai_feature * u.invited_team_member)::BIGINT AS activated_14d,
    (u.signup_date + 14 <= d.last_event_date) AS window_14d_complete
FROM {{ ref('int_user_signup_plan') }} u
CROSS JOIN data_end d
GROUP BY u.signup_date, u.plan_tier, d.last_event_date
