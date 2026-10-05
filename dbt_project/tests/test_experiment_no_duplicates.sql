-- Test: no user should be assigned to the same experiment twice
SELECT experiment_id, user_id, COUNT(*) AS cnt
FROM {{ ref('fct_experiment_assignments') }}
GROUP BY experiment_id, user_id
HAVING COUNT(*) > 1
