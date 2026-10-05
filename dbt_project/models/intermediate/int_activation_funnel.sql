-- Intermediate: tracks users through the 4-stage activation funnel
-- Signup → First Call → AI Feature Used → Team Invited (within 14 days)
WITH users AS (
    SELECT user_id, signup_date
    FROM {{ ref('stg_users') }}
),
events AS (
    SELECT user_id, event_name, event_date
    FROM {{ ref('stg_events') }}
),
milestones AS (
    SELECT
        u.user_id,
        u.signup_date,
        MAX(CASE WHEN e.event_name = 'call.started'
            AND e.event_date <= u.signup_date + INTERVAL '14 days'
            THEN 1 ELSE 0 END) AS placed_first_call,
        MAX(CASE WHEN e.event_name IN ('ai_assist.used', 'ai_voice_agent.activated')
            AND e.event_date <= u.signup_date + INTERVAL '14 days'
            THEN 1 ELSE 0 END) AS used_ai_feature,
        MAX(CASE WHEN e.event_name = 'team.member_invited'
            AND e.event_date <= u.signup_date + INTERVAL '14 days'
            THEN 1 ELSE 0 END) AS invited_team_member,
        MIN(CASE WHEN e.event_name = 'call.started'
            THEN e.event_date END) AS first_call_date,
        MIN(CASE WHEN e.event_name IN ('ai_assist.used', 'ai_voice_agent.activated')
            THEN e.event_date END) AS first_ai_date,
        MIN(CASE WHEN e.event_name = 'team.member_invited'
            THEN e.event_date END) AS first_invite_date
    FROM users u
    LEFT JOIN events e ON u.user_id = e.user_id
    GROUP BY u.user_id, u.signup_date
)
SELECT
    user_id,
    signup_date,
    1 AS signed_up,
    placed_first_call,
    used_ai_feature,
    invited_team_member,
    CASE
        WHEN invited_team_member = 1 THEN 'fully_activated'
        WHEN used_ai_feature = 1 THEN 'ai_activated'
        WHEN placed_first_call = 1 THEN 'call_activated'
        ELSE 'signed_up_only'
    END AS activation_stage,
    first_call_date,
    first_ai_date,
    first_invite_date
FROM milestones
