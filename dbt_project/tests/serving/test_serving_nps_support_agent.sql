-- NPS, support and AI-agent serving models: category reconciliation, totals
-- against their sources, and daily active users against fct_daily_active_users.
-- Returns one row per violation.
{{ config(tags=['serving']) }}
SELECT 'nps_categories' AS failure, response_date::TEXT AS detail
FROM {{ ref('fct_nps_daily') }}
WHERE promoters + passives + detractors <> responses OR responses <= 0
   OR score_sum NOT BETWEEN 0 AND 10 * responses
UNION ALL
SELECT 'nps_total', (SELECT SUM(responses) FROM {{ ref('fct_nps_daily') }})::TEXT
WHERE (SELECT SUM(responses) FROM {{ ref('fct_nps_daily') }})
      <> (SELECT COUNT(*) FROM {{ ref('stg_nps_responses') }})
UNION ALL
SELECT 'nps_score_sum', ''
WHERE (SELECT SUM(score_sum) FROM {{ ref('fct_nps_daily') }})
      <> (SELECT SUM(score) FROM {{ ref('stg_nps_responses') }})
UNION ALL
SELECT 'agent_outcomes', call_date::TEXT || ' ' || call_type
FROM {{ ref('fct_agent_performance_daily') }}
WHERE ai_resolved + escalated + human_handled <> calls OR calls <= 0
   OR csat_count > calls OR handle_time_count > calls
   OR (csat_count > 0 AND csat_sum NOT BETWEEN 1 * csat_count AND 5 * csat_count)
   OR handle_time_seconds_sum < 0
UNION ALL
SELECT 'agent_total', ''
WHERE (SELECT SUM(calls) FROM {{ ref('fct_agent_performance_daily') }})
      <> (SELECT COUNT(*) FROM {{ ref('fct_agent_evaluations') }})
UNION ALL
SELECT 'support_counts', event_date::TEXT || ' ' || plan_tier
FROM {{ ref('fct_support_daily') }}
WHERE active_users <= 0 OR tickets_created < 0 OR tickets_resolved < 0
UNION ALL
SELECT 'support_tickets_total', ''
WHERE (SELECT SUM(tickets_created) FROM {{ ref('fct_support_daily') }})
      <> (SELECT COUNT(*) FROM {{ ref('stg_events') }} WHERE event_name = 'support.ticket_created')
   OR (SELECT SUM(tickets_resolved) FROM {{ ref('fct_support_daily') }})
      <> (SELECT COUNT(*) FROM {{ ref('stg_events') }} WHERE event_name = 'support.ticket_resolved')
UNION ALL
-- a user has one workspace and one billed plan per month, so per-plan daily
-- active users add up to DAU
SELECT 'support_active_users_vs_dau', COALESCE(s.event_date, d.event_date)::TEXT
FROM (SELECT event_date, SUM(active_users) AS users
      FROM {{ ref('fct_support_daily') }} GROUP BY event_date) s
FULL JOIN {{ ref('fct_daily_active_users') }} d ON d.event_date = s.event_date
WHERE COALESCE(s.users, 0) <> COALESCE(d.dau, 0)
