-- Gold: cumulative feature adoption by days since signup (days 0-90)
-- A user adopts a feature on the first day they use it, so each user is
-- counted at most once per feature and cumulative_adoption_pct <= 100.
--
-- cumulative_adoption_pct divides by ALL users, including users who signed up
-- fewer than D days before the last loaded event date and so could not yet be
-- observed at day D: it is right-censored and understates late-day adoption.
-- The observed_* columns are the censoring-aware alternative: at day D they only
-- count users with at least D days of history (signup_date + D <= last event date).
--   eligible_users          users observable at day D
--   eligible_adopters       of those, users who adopted the feature by day D
--   observed_adoption_pct   eligible_adopters / eligible_users (NULL if none eligible)
WITH users AS (
    SELECT user_id, signup_date
    FROM {{ ref('stg_users') }}
),
data_end AS (
    SELECT MAX(event_date) AS last_event_date FROM {{ ref('stg_events') }}
),
first_use AS (
    SELECT
        fu.feature_name,
        fu.user_id,
        u.signup_date,
        MIN(fu.event_date - u.signup_date) AS first_use_day  -- DATE - DATE = INTEGER
    FROM {{ ref('int_feature_usage') }} fu
    JOIN users u ON fu.user_id = u.user_id
    WHERE fu.event_date >= u.signup_date
    GROUP BY fu.feature_name, fu.user_id, u.signup_date
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
),
signups_by_date AS (
    SELECT signup_date, COUNT(*) AS users FROM users GROUP BY signup_date
),
eligible AS (
    SELECT d.days_since_signup, COALESCE(SUM(s.users), 0) AS eligible_users
    FROM generate_series(0, 90) AS d(days_since_signup)
    CROSS JOIN data_end de
    LEFT JOIN signups_by_date s ON s.signup_date + d.days_since_signup <= de.last_event_date
    GROUP BY d.days_since_signup
),
adopters_by_signup AS (
    SELECT feature_name, signup_date, first_use_day, COUNT(*) AS users
    FROM first_use
    WHERE first_use_day <= 90
    GROUP BY feature_name, signup_date, first_use_day
),
eligible_adopters AS (
    SELECT s.feature_name, s.days_since_signup, COALESCE(SUM(a.users), 0) AS eligible_adopters
    FROM day_spine s
    CROSS JOIN data_end de
    LEFT JOIN adopters_by_signup a
        ON a.feature_name = s.feature_name
       AND a.first_use_day <= s.days_since_signup
       AND a.signup_date + s.days_since_signup <= de.last_event_date
    GROUP BY s.feature_name, s.days_since_signup
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
    ) AS cumulative_adoption_pct,
    e.eligible_users::BIGINT AS eligible_users,
    ea.eligible_adopters::BIGINT AS eligible_adopters,
    ROUND(ea.eligible_adopters * 100.0 / NULLIF(e.eligible_users, 0), 2) AS observed_adoption_pct
FROM day_spine s
LEFT JOIN new_adopters n
    ON n.feature_name = s.feature_name
   AND n.days_since_signup = s.days_since_signup
CROSS JOIN total_users tu
JOIN eligible e ON e.days_since_signup = s.days_since_signup
JOIN eligible_adopters ea
    ON ea.feature_name = s.feature_name
   AND ea.days_since_signup = s.days_since_signup
ORDER BY s.feature_name, s.days_since_signup
