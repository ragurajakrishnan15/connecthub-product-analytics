-- Gold (serving): monthly recurring revenue per billed plan, with the MRR
-- movement split into components.
--
-- Movement amounts are Σ mrr_change_usd per movement type from fct_workspace_mrr:
--   new, reactivation, expansion   >= 0
--   contraction, churned           <= 0
--   retained, inactive             = 0 by definition (not stored)
-- so new + expansion + reactivation + contraction + churned = mrr_usd - previous_mrr_usd.
-- previous_mrr_usd is the previous month's MRR of the workspaces on this plan
-- *this* month: a workspace that changed tier moves all of its MRR to the new
-- tier, so the cross-month identity holds for the all-plans total, not per tier.
-- month_complete is FALSE when the month extends past the last loaded event date
-- (billing for that month is still accumulating).
WITH data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
)
SELECT
    m.month_start,
    m.plan_tier,
    COUNT(*)::BIGINT AS workspaces,
    COUNT(*) FILTER (WHERE m.mrr_usd > 0)::BIGINT AS paying_workspaces,
    SUM(m.billed_seats)::BIGINT AS billed_seats,
    SUM(m.mrr_usd)::NUMERIC AS mrr_usd,
    SUM(m.previous_mrr_usd)::NUMERIC AS previous_mrr_usd,
    COALESCE(SUM(m.mrr_change_usd) FILTER (WHERE m.mrr_movement = 'new'), 0)::NUMERIC
        AS new_mrr_usd,
    COALESCE(SUM(m.mrr_change_usd) FILTER (WHERE m.mrr_movement = 'expansion'), 0)::NUMERIC
        AS expansion_mrr_usd,
    COALESCE(SUM(m.mrr_change_usd) FILTER (WHERE m.mrr_movement = 'reactivation'), 0)::NUMERIC
        AS reactivation_mrr_usd,
    COALESCE(SUM(m.mrr_change_usd) FILTER (WHERE m.mrr_movement = 'contraction'), 0)::NUMERIC
        AS contraction_mrr_usd,
    COALESCE(SUM(m.mrr_change_usd) FILTER (WHERE m.mrr_movement = 'churned'), 0)::NUMERIC
        AS churned_mrr_usd,
    COUNT(*) FILTER (WHERE m.mrr_movement = 'new')::BIGINT AS new_workspaces,
    COUNT(*) FILTER (WHERE m.mrr_movement = 'expansion')::BIGINT AS expansion_workspaces,
    COUNT(*) FILTER (WHERE m.mrr_movement = 'reactivation')::BIGINT AS reactivation_workspaces,
    COUNT(*) FILTER (WHERE m.mrr_movement = 'contraction')::BIGINT AS contraction_workspaces,
    COUNT(*) FILTER (WHERE m.mrr_movement = 'churned')::BIGINT AS churned_workspaces,
    ((m.month_start + INTERVAL '1 month' - INTERVAL '1 day')::DATE <= d.last_event_date)
        AS month_complete
FROM {{ ref('fct_workspace_mrr') }} m
CROSS JOIN data_end d
GROUP BY m.month_start, m.plan_tier, d.last_event_date
