-- Test: retention rate should be between 0 and 100
SELECT cohort_week, weeks_since_signup, retention_rate
FROM {{ ref('fct_retention_cohorts') }}
WHERE retention_rate < 0 OR retention_rate > 100
