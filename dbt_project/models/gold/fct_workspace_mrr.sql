-- Gold: monthly recurring revenue per workspace with month-over-month movement.
-- Billing is per active seat: billed_seats = users with any activity that month.
WITH subs AS (
    SELECT
        *,
        LAG(mrr_usd) OVER (PARTITION BY workspace_id ORDER BY month_start) AS prev_mrr_usd
    FROM {{ ref('stg_subscriptions') }}
)
SELECT
    workspace_id,
    month_start,
    plan_tier,
    billed_seats,
    seat_price_usd,
    mrr_usd,
    COALESCE(prev_mrr_usd, 0) AS previous_mrr_usd,
    mrr_usd - COALESCE(prev_mrr_usd, 0) AS mrr_change_usd,
    CASE
        WHEN prev_mrr_usd IS NULL AND mrr_usd > 0 THEN 'new'
        WHEN prev_mrr_usd = 0 AND mrr_usd > 0 THEN 'reactivation'
        WHEN prev_mrr_usd > 0 AND mrr_usd = 0 THEN 'churned'
        WHEN mrr_usd > prev_mrr_usd THEN 'expansion'
        WHEN mrr_usd < prev_mrr_usd THEN 'contraction'
        WHEN mrr_usd > 0 THEN 'retained'
        ELSE 'inactive'
    END AS mrr_movement
FROM subs
