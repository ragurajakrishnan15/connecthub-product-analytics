-- Gold: experiment variant assignments with exposure timestamps
-- This table is populated by the experimentation/assignment.py engine
-- and consumed by the experiment evaluation pipeline
SELECT
    experiment_id,
    user_id,
    variant,
    assigned_at,
    DATE(assigned_at) AS assignment_date
FROM {{ source('experiments', 'experiment_assignments') }}
WHERE experiment_id IS NOT NULL
  AND user_id IS NOT NULL
  AND variant IS NOT NULL
