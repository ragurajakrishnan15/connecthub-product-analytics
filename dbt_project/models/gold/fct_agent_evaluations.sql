-- Gold: AI agent performance metrics by call type
SELECT
    eval_id,
    workspace_id,
    call_date,
    call_type,
    resolved_by_ai,
    handle_time_seconds,
    csat_score,
    escalated_to_human,
    CASE
        WHEN resolved_by_ai AND NOT escalated_to_human THEN 'ai_resolved'
        WHEN escalated_to_human THEN 'escalated'
        ELSE 'human_handled'
    END AS resolution_path
FROM {{ source('bronze', 'agent_evaluations') }}
WHERE eval_id IS NOT NULL
