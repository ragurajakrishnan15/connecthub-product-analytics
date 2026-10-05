-- Test: cumulative adoption is a percentage of users, so it must stay in [0, 100]
-- and never decrease as days_since_signup grows.
WITH ordered AS (
    SELECT
        feature_name,
        days_since_signup,
        cumulative_adoption_pct,
        LAG(cumulative_adoption_pct) OVER (
            PARTITION BY feature_name ORDER BY days_since_signup
        ) AS prev_pct
    FROM {{ ref('fct_feature_adoption') }}
)
SELECT *
FROM ordered
WHERE cumulative_adoption_pct < 0
   OR cumulative_adoption_pct > 100
   OR cumulative_adoption_pct < prev_pct
