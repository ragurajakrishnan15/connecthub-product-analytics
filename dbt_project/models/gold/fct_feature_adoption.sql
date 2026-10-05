-- Gold: cumulative feature adoption by days since signup (days 0-90)
-- A user adopts a feature on the first day they use it, so each user is
-- counted at most once per feature and cumulative_adoption_pct <= 100.
WITH users AS (
    SELECT user_id, signup_date
    FROM {{ ref('stg_users') }}
),
first_use AS (
    SELECT
        fu.feature_name,
        fu.user_id,
        MIN(fu.event_date - u.signup_date) AS first_use_day  -- DATE - DATE = INTEGER
    FROM {{ ref('int_feature_usage') }} fu
    JOIN users u ON fu.user_id = u.user_id
    WHERE fu.event_date >= u.signup_date
    GROUP BY fu.feature_name, fu.user_id
),
new_adopters AS (
    SELECT
        feature_name,
        first_use_day AS days_since_signup,
        COUNT(*) AS users_adopted
    FROM first_use
    WHERE first_use_day <= 90
    GROUP BY feature_name, first_use_day
),
day_spine AS (
    SELECT f.feature_name, d.days_since_signup
    FROM (SELECT DISTINCT feature_name FROM first_use) f
    CROSS JOIN generate_series(0, 90) AS d(days_since_signup)
),
total_users AS (
    SELECT COUNT(DISTINCT user_id) AS total FROM users
)
SELECT
    s.feature_name,
    s.days_since_signup,
    COALESCE(n.users_adopted, 0) AS users_adopted,  -- users first adopting on this day
    tu.total AS total_users,
    ROUND(
        SUM(COALESCE(n.users_adopted, 0)) OVER (
            PARTITION BY s.feature_name
            ORDER BY s.days_since_signup
        ) * 100.0 / NULLIF(tu.total, 0), 2
    ) AS cumulative_adoption_pct
FROM day_spine s
LEFT JOIN new_adopters n
    ON n.feature_name = s.feature_name
   AND n.days_since_signup = s.days_since_signup
CROSS JOIN total_users tu
ORDER BY s.feature_name, s.days_since_signup
