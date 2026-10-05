-- Staging: deduplicated, typed, null-checked raw events
WITH raw AS (
    SELECT * FROM {{ source('bronze', 'events_raw') }}
),
deduplicated AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY event_id
            ORDER BY timestamp_utc DESC
        ) AS row_num
    FROM raw
)
SELECT
    event_id,
    user_id,
    workspace_id,
    event_name,
    timestamp_utc,
    event_date::DATE AS event_date,
    session_id,
    platform,
    country_code
FROM deduplicated
WHERE row_num = 1
  AND event_id IS NOT NULL
  AND user_id IS NOT NULL
  AND event_name IS NOT NULL
