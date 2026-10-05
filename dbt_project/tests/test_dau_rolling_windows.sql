-- Test: the linear-time WAU/MAU in fct_daily_active_users matches a brute-force
-- distinct count over the window, checked on the 1st and 15th of every month.
WITH daily AS (
    SELECT DISTINCT event_date, user_id FROM {{ ref('stg_events') }}
),
checked AS (
    SELECT event_date, wau_7d, mau_28d
    FROM {{ ref('fct_daily_active_users') }}
    WHERE EXTRACT(DAY FROM event_date) IN (1, 15)
),
brute AS (
    SELECT
        c.event_date,
        COUNT(DISTINCT d.user_id) FILTER (WHERE d.event_date > c.event_date - 7) AS wau_7d,
        COUNT(DISTINCT d.user_id) AS mau_28d
    FROM checked c
    JOIN daily d ON d.event_date BETWEEN c.event_date - 27 AND c.event_date
    GROUP BY c.event_date
)
SELECT c.event_date, c.wau_7d, b.wau_7d AS expected_wau, c.mau_28d, b.mau_28d AS expected_mau
FROM checked c
LEFT JOIN brute b ON b.event_date = c.event_date
WHERE c.wau_7d <> COALESCE(b.wau_7d, 0)
   OR c.mau_28d <> COALESCE(b.mau_28d, 0)
