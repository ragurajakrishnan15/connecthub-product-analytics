-- fct_revenue_monthly: signs, movement identity, cross-month continuity and
-- totals. Returns one row per violation.
{{ config(tags=['serving']) }}
WITH r AS (
    SELECT * FROM {{ ref('fct_revenue_monthly') }}
),
totals AS (
    SELECT month_start, SUM(mrr_usd) AS mrr_usd, SUM(previous_mrr_usd) AS previous_mrr_usd
    FROM r GROUP BY month_start
)
-- no negative revenue; movement components have their defined signs
SELECT 'signs' AS failure, month_start::TEXT || ' ' || plan_tier AS detail
FROM r
WHERE mrr_usd < 0 OR previous_mrr_usd < 0 OR new_mrr_usd < 0 OR expansion_mrr_usd < 0
   OR reactivation_mrr_usd < 0 OR contraction_mrr_usd > 0 OR churned_mrr_usd > 0
   OR paying_workspaces > workspaces OR billed_seats < 0
UNION ALL
-- components sum to the month-over-month change (per row)
SELECT 'movement_identity', month_start::TEXT || ' ' || plan_tier
FROM r
WHERE new_mrr_usd + expansion_mrr_usd + reactivation_mrr_usd + contraction_mrr_usd
      + churned_mrr_usd <> mrr_usd - previous_mrr_usd
UNION ALL
-- all-plans previous MRR equals last month's all-plans MRR (no workspace vanishes)
SELECT 'cross_month', t.month_start::TEXT
FROM totals t
JOIN totals p ON p.month_start = (t.month_start - INTERVAL '1 month')::DATE
WHERE t.previous_mrr_usd <> p.mrr_usd
UNION ALL
-- the first month has no previous MRR
SELECT 'first_month_previous', month_start::TEXT
FROM totals
WHERE month_start = (SELECT MIN(month_start) FROM totals) AND previous_mrr_usd <> 0
UNION ALL
-- totals match the workspace-level model
SELECT 'mrr_total', (SELECT SUM(mrr_usd) FROM r)::TEXT
WHERE (SELECT SUM(mrr_usd) FROM r) <> (SELECT SUM(mrr_usd) FROM {{ ref('fct_workspace_mrr') }})
