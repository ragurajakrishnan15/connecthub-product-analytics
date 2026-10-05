-- Staging: deduplicated, typed, null-checked raw events.
-- Incremental: each run reprocesses bronze events dated within the lookback
-- window (macros/incremental_window.sql) and replaces them by event_id.
-- Materialized once here so downstream models stop re-running the dedupe.
{{ config(
    materialized='incremental',
    unique_key='event_id',
    incremental_strategy='delete+insert',
    on_schema_change='fail',
    indexes=[
        {'columns': ['event_id'], 'unique': True},
        {'columns': ['user_id']},
        {'columns': ['event_date']},
        {'columns': ['session_id']},
    ]
) }}
WITH raw AS (
    SELECT * FROM {{ source('bronze', 'events_raw') }}
    {% if is_incremental() %}
    WHERE event_date >= {{ incremental_window_start('event_date') }}
    {% endif %}
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
