-- Gold: weekly retention matrix (week 0 through week 12)
WITH cohorts AS (
    SELECT
        user_id,
        DATE_TRUNC('week', first_active_date)::DATE AS cohort_week
    FROM {{ ref('stg_users') }}
    WHERE first_active_date IS NOT NULL
),
activity AS (
    SELECT DISTINCT
        user_id,
        DATE_TRUNC('week', event_date)::DATE AS activity_week
    FROM {{ ref('stg_events') }}
),
retention AS (
    SELECT
        c.cohort_week,
        (a.activity_week - c.cohort_week) / 7  -- DATE - DATE = days (INTEGER)
            AS weeks_since_signup,
        COUNT(DISTINCT a.user_id) AS active_users,
        (SELECT COUNT(DISTINCT c2.user_id)
         FROM cohorts c2
         WHERE c2.cohort_week = c.cohort_week) AS cohort_size
    FROM cohorts c
    LEFT JOIN activity a ON c.user_id = a.user_id
    WHERE a.activity_week >= c.cohort_week
    GROUP BY c.cohort_week,
             (a.activity_week - c.cohort_week) / 7
)
SELECT
    cohort_week,
    weeks_since_signup,
    active_users,
    cohort_size,
    ROUND(active_users * 100.0 / NULLIF(cohort_size, 0), 2) AS retention_rate
FROM retention
WHERE weeks_since_signup BETWEEN 0 AND 12
ORDER BY cohort_week, weeks_since_signup
