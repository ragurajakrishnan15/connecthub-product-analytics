-- Staging: monthly per-active-seat billing per workspace (one row per workspace-month)
SELECT
    workspace_id,
    month_start::DATE AS month_start,
    plan_tier,
    billed_seats,
    seat_price_usd,
    mrr_usd
FROM {{ source('bronze', 'subscriptions') }}
WHERE workspace_id IS NOT NULL
  AND month_start IS NOT NULL
