-- Gold (serving): cumulative 14-day activation by day since signup, per
-- experiment arm. Only participants with a complete 14-day window are counted,
-- the same population experimentation/evaluate.py uses for the primary metric,
-- so day 14 equals the evaluated control/treatment activation rate.
-- A user's activation day is the day the last of the three milestones was
-- reached. users_in_window > 0 on every row (arms come from participants).
WITH participants AS (
    SELECT
        m.experiment_id,
        m.variant,
        m.user_id,
        CASE WHEN m.activated_14d = 1
             THEN GREATEST(f.first_call_date, f.first_ai_date, f.first_invite_date) - m.signup_date
        END AS activation_day
    FROM {{ ref('fct_experiment_user_metrics') }} m
    JOIN {{ ref('int_activation_funnel') }} f ON f.user_id = m.user_id
    WHERE m.window_14d_complete
),
arms AS (
    SELECT experiment_id, variant, COUNT(*) AS users_in_window
    FROM participants
    GROUP BY experiment_id, variant
),
activations AS (
    SELECT experiment_id, variant, activation_day, COUNT(*) AS activated
    FROM participants
    WHERE activation_day IS NOT NULL
    GROUP BY experiment_id, variant, activation_day
),
curve AS (
    SELECT
        a.experiment_id,
        a.variant,
        d.day_since_signup,
        a.users_in_window,
        SUM(COALESCE(x.activated, 0)) OVER (
            PARTITION BY a.experiment_id, a.variant ORDER BY d.day_since_signup
        ) AS activated_cumulative
    FROM arms a
    CROSS JOIN generate_series(0, 14) AS d(day_since_signup)
    LEFT JOIN activations x
        ON x.experiment_id = a.experiment_id
       AND x.variant = a.variant
       AND x.activation_day = d.day_since_signup
)
SELECT
    experiment_id,
    variant,
    day_since_signup,
    users_in_window::BIGINT AS users_in_window,
    activated_cumulative::BIGINT AS activated_cumulative,
    ROUND(activated_cumulative::NUMERIC / users_in_window, 6) AS cumulative_activation_rate
FROM curve
