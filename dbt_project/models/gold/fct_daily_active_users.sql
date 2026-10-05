-- Gold: DAU/WAU/MAU with rolling windows
WITH daily AS (
    SELECT
        event_date,
        user_id,
        workspace_id
    FROM {{ ref('stg_events') }}
    GROUP BY event_date, user_id, workspace_id
),
dau AS (
    SELECT
        event_date,
        COUNT(DISTINCT user_id) AS dau,
        COUNT(DISTINCT workspace_id) AS active_workspaces
    FROM daily
    GROUP BY event_date
)
SELECT
    d.event_date,
    d.dau,
    d.active_workspaces,
    -- 7-day rolling WAU
    (
        SELECT COUNT(DISTINCT d2.user_id)
        FROM daily d2
        WHERE d2.event_date BETWEEN d.event_date - INTERVAL '6 days' AND d.event_date
    ) AS wau_7d,
    -- 28-day rolling MAU
    (
        SELECT COUNT(DISTINCT d3.user_id)
        FROM daily d3
        WHERE d3.event_date BETWEEN d.event_date - INTERVAL '27 days' AND d.event_date
    ) AS mau_28d
FROM dau d
ORDER BY d.event_date
