-- Gold: DAU/WAU/MAU with rolling windows, one row per calendar day.
--
-- Scales linearly with user-days: each active user-day "covers" the following
-- 7 (or 28) days until that user's next active day, so a user's coverage
-- intervals never overlap. WAU on day D = intervals covering D, computed as a
-- running sum of +1 at each interval start and -1 at its end.
WITH daily AS (
    SELECT DISTINCT event_date, user_id
    FROM {{ ref('stg_events') }}
),
with_next AS (
    SELECT
        event_date,
        LEAD(event_date) OVER (PARTITION BY user_id ORDER BY event_date) AS next_active_date
    FROM daily
),
deltas AS (
    SELECT event_date AS delta_date, 1 AS wau_delta, 1 AS mau_delta FROM with_next
    UNION ALL
    SELECT LEAST(next_active_date, event_date + 7), -1, 0 FROM with_next
    UNION ALL
    SELECT LEAST(next_active_date, event_date + 28), 0, -1 FROM with_next
),
delta_by_date AS (
    SELECT delta_date, SUM(wau_delta) AS wau_delta, SUM(mau_delta) AS mau_delta
    FROM deltas
    GROUP BY delta_date
),
date_spine AS (
    SELECT generate_series(MIN(event_date), MAX(event_date), INTERVAL '1 day')::DATE AS event_date
    FROM daily
),
dau AS (
    SELECT
        event_date,
        COUNT(DISTINCT user_id) AS dau,
        COUNT(DISTINCT workspace_id) AS active_workspaces
    FROM {{ ref('stg_events') }}
    GROUP BY event_date
)
SELECT
    s.event_date,
    COALESCE(d.dau, 0) AS dau,
    COALESCE(d.active_workspaces, 0) AS active_workspaces,
    SUM(COALESCE(x.wau_delta, 0)) OVER (ORDER BY s.event_date) AS wau_7d,
    SUM(COALESCE(x.mau_delta, 0)) OVER (ORDER BY s.event_date) AS mau_28d
FROM date_spine s
LEFT JOIN dau d ON d.event_date = s.event_date
LEFT JOIN delta_by_date x ON x.delta_date = s.event_date
ORDER BY s.event_date
