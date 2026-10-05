-- Intermediate (ephemeral): each user's activation milestones with the plan
-- their workspace was billed on in the signup month.
--
-- "Plan" in the serving models is always the billed plan of the month the
-- activity happened in (stg_subscriptions), falling back to the workspace's
-- current plan when the workspace has no subscription row for that month.
-- stg_users.plan_tier is the workspace's *current* plan, which differs from the
-- signup-month plan for workspaces that changed tier.
{{ config(materialized='ephemeral') }}
SELECT
    f.user_id,
    f.signup_date,
    COALESCE(s.plan_tier, w.plan_tier) AS plan_tier,
    f.placed_first_call,
    f.used_ai_feature,
    f.invited_team_member,
    f.first_call_date,
    f.first_ai_date,
    f.first_invite_date
FROM {{ ref('int_activation_funnel') }} f
JOIN {{ ref('stg_users') }} u ON u.user_id = f.user_id
LEFT JOIN {{ ref('stg_workspaces') }} w ON w.workspace_id = u.workspace_id
LEFT JOIN {{ ref('stg_subscriptions') }} s
    ON s.workspace_id = u.workspace_id
   AND s.month_start = DATE_TRUNC('month', f.signup_date)::DATE
