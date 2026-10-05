-- Intermediate: daily feature usage aggregates per user per feature.
-- Incremental: whole dates in the lookback window are recomputed, which is
-- exact because event_date is part of the grain.
{{ config(
    materialized='incremental',
    unique_key=['user_id', 'workspace_id', 'event_date', 'feature_name'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
    indexes=[
        {'columns': ['user_id', 'workspace_id', 'event_date', 'feature_name'], 'unique': True},
        {'columns': ['workspace_id', 'event_date']},
    ]
) }}
WITH events AS (
    SELECT * FROM {{ ref('stg_events') }}
    {% if is_incremental() %}
    WHERE event_date >= {{ incremental_window_start('event_date') }}
    {% endif %}
),
feature_mapped AS (
    SELECT
        user_id,
        workspace_id,
        event_date,
        CASE
            WHEN event_name IN ('ai_assist.used', 'ai_assist.summary_generated')
                THEN 'ai_assist'
            WHEN event_name IN ('ai_voice_agent.activated', 'ai_voice_agent.call_handled')
                THEN 'ai_voice_agent'
            WHEN event_name IN ('call.started', 'call.ended')
                THEN 'voice_calls'
            WHEN event_name = 'call.recorded'
                THEN 'call_recording'
            WHEN event_name IN ('sms.sent', 'sms.received')
                THEN 'sms'
            WHEN event_name IN ('whatsapp.sent', 'whatsapp.received')
                THEN 'whatsapp'
            WHEN event_name IN ('integration.installed', 'integration.configured')
                THEN 'integrations'
            ELSE NULL
        END AS feature_name
    FROM events
)
SELECT
    user_id,
    workspace_id,
    event_date,
    feature_name,
    COUNT(*) AS usage_count
FROM feature_mapped
WHERE feature_name IS NOT NULL
GROUP BY user_id, workspace_id, event_date, feature_name
