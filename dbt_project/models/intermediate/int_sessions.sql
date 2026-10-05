-- Intermediate: sessionized events with session duration and event count
WITH events AS (
    SELECT * FROM {{ ref('stg_events') }}
)
SELECT
    session_id,
    user_id,
    workspace_id,
    MIN(timestamp_utc) AS session_start,
    MAX(timestamp_utc) AS session_end,
    EXTRACT(EPOCH FROM (MAX(timestamp_utc) - MIN(timestamp_utc))) / 60.0
        AS session_duration_min,
    COUNT(*) AS event_count,
    MIN(event_date) AS session_date,
    COUNT(DISTINCT event_name) AS distinct_events
FROM events
GROUP BY session_id, user_id, workspace_id
