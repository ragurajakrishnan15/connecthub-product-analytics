-- Gold (serving): AI voice-agent call outcomes per call date, billed plan and call type.
--
-- One row of fct_agent_evaluations is one AI-handled call, so:
--   AI resolution rate = ai_resolved / calls  (resolved by the agent without escalation)
--   escalation rate    = escalated / calls
-- This is a share of AI-handled calls. It differs from metrics_product_health's
-- pct_ai_calls_automated, whose denominator also includes human-placed calls.
-- CSAT and handle time are stored as sums with their non-null counts so means
-- over any range are exact weighted means. A missing call_type is reported as 'unknown'.
SELECT
    a.call_date,
    COALESCE(s.plan_tier, w.plan_tier) AS plan_tier,
    COALESCE(a.call_type, 'unknown') AS call_type,
    COUNT(*)::BIGINT AS calls,
    COUNT(*) FILTER (WHERE a.resolution_path = 'ai_resolved')::BIGINT AS ai_resolved,
    COUNT(*) FILTER (WHERE a.resolution_path = 'escalated')::BIGINT AS escalated,
    COUNT(*) FILTER (WHERE a.resolution_path = 'human_handled')::BIGINT AS human_handled,
    COALESCE(SUM(a.csat_score), 0)::NUMERIC AS csat_sum,
    COUNT(a.csat_score)::BIGINT AS csat_count,
    COALESCE(SUM(a.handle_time_seconds), 0)::BIGINT AS handle_time_seconds_sum,
    COUNT(a.handle_time_seconds)::BIGINT AS handle_time_count
FROM {{ ref('fct_agent_evaluations') }} a
LEFT JOIN {{ ref('stg_workspaces') }} w ON w.workspace_id = a.workspace_id
LEFT JOIN {{ ref('stg_subscriptions') }} s
    ON s.workspace_id = a.workspace_id
   AND s.month_start = DATE_TRUNC('month', a.call_date)::DATE
GROUP BY a.call_date, COALESCE(s.plan_tier, w.plan_tier), COALESCE(a.call_type, 'unknown')
