-- Test: no null event_ids in staging events
SELECT event_id
FROM {{ ref('stg_events') }}
WHERE event_id IS NULL
