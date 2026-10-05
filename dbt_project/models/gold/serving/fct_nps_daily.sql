-- Gold (serving): NPS survey responses per response date and billed plan.
-- Additive counts; NPS for any range = 100 * (promoters - detractors) / responses
-- (docs/metric-definitions.md). score_sum supports the mean score.
SELECT
    n.response_date,
    COALESCE(s.plan_tier, w.plan_tier) AS plan_tier,
    COUNT(*)::BIGINT AS responses,
    COUNT(*) FILTER (WHERE n.nps_category = 'promoter')::BIGINT AS promoters,
    COUNT(*) FILTER (WHERE n.nps_category = 'passive')::BIGINT AS passives,
    COUNT(*) FILTER (WHERE n.nps_category = 'detractor')::BIGINT AS detractors,
    SUM(n.score)::BIGINT AS score_sum
FROM {{ ref('stg_nps_responses') }} n
LEFT JOIN {{ ref('stg_workspaces') }} w ON w.workspace_id = n.workspace_id
LEFT JOIN {{ ref('stg_subscriptions') }} s
    ON s.workspace_id = n.workspace_id
   AND s.month_start = DATE_TRUNC('month', n.response_date)::DATE
GROUP BY n.response_date, COALESCE(s.plan_tier, w.plan_tier)
