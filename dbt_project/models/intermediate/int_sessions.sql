-- Intermediate: sessionized events with session duration and event count.
-- Incremental: sessions with any event in the lookback window are recomputed
-- from all of their events, so a session that straddles the window boundary
-- is still exact.
{{ config(
    materialized='incremental',
    unique_key='session_id',
    incremental_strategy='delete+insert',
    on_schema_change='fail',
    indexes=[
        {'columns': ['session_id'], 'unique': True},
        {'columns': ['user_id']},
        {'columns': ['session_date']},
    ]
) }}
WITH events AS (
    SELECT e.*
    FROM {{ ref('stg_events') }} e
    {% if is_incremental() %}
    WHERE e.session_id IN (
        SELECT session_id
        FROM {{ ref('stg_events') }}
        WHERE event_date >= {{ incremental_window_start('session_date') }}
    )
    {% endif %}
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
