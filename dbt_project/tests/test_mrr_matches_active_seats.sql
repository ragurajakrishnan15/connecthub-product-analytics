-- Test: billing is per active seat, so each workspace-month's billed_seats must
-- equal its users with activity that month, and MRR = seats x seat price.
-- Only months fully covered by the loaded events are compared (during a daily
-- incremental load the current month is still partial).
WITH loaded AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
),
active_seats AS (
    SELECT workspace_id, DATE_TRUNC('month', event_date)::DATE AS month_start,
           COUNT(DISTINCT user_id) AS active_users
    FROM {{ ref('stg_events') }}
    GROUP BY 1, 2
)
SELECT s.workspace_id, s.month_start, s.billed_seats, COALESCE(a.active_users, 0) AS active_users
FROM {{ ref('stg_subscriptions') }} s
CROSS JOIN loaded l
LEFT JOIN active_seats a
    ON a.workspace_id = s.workspace_id AND a.month_start = s.month_start
WHERE (s.month_start + INTERVAL '1 month' - INTERVAL '1 day')::DATE <= l.last_event_date
  AND (s.billed_seats <> COALESCE(a.active_users, 0)
       OR s.mrr_usd <> s.billed_seats * s.seat_price_usd)
