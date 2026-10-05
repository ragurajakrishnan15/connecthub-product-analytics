"""gold.fct_activation_daily and gold.fct_activation_milestone_days."""
from sqlalchemy import text

SOURCES = ('gold.fct_activation_daily', 'gold.fct_activation_milestone_days')

_FILTER = """signup_date BETWEEN :start AND :end
  AND (CAST(:plan_tier AS text) IS NULL OR plan_tier = :plan_tier)
  AND (:include_incomplete OR window_14d_complete)"""


def _params(start, end, plan_tier, include_incomplete, **extra):
    return {'start': start, 'end': end, 'plan_tier': plan_tier,
            'include_incomplete': include_incomplete, **extra}


def totals(conn, start, end, plan_tier, include_incomplete):
    row = conn.execute(text(f"""
        SELECT COALESCE(SUM(signups), 0)::bigint AS signups,
               COALESCE(SUM(placed_first_call), 0)::bigint AS placed_first_call,
               COALESCE(SUM(used_ai_feature), 0)::bigint AS used_ai_feature,
               COALESCE(SUM(invited_team_member), 0)::bigint AS invited_team_member,
               COALESCE(SUM(call_and_ai), 0)::bigint AS call_and_ai,
               COALESCE(SUM(activated_14d), 0)::bigint AS activated_14d
        FROM gold.fct_activation_daily
        WHERE {_FILTER}"""), _params(start, end, plan_tier, include_incomplete)).mappings().one()
    return dict(row)


def incomplete_signups(conn, start, end, plan_tier):
    return conn.execute(text("""
        SELECT COALESCE(SUM(signups), 0)::bigint FROM gold.fct_activation_daily
        WHERE signup_date BETWEEN :start AND :end AND NOT window_14d_complete
          AND (CAST(:plan_tier AS text) IS NULL OR plan_tier = :plan_tier)"""),
        {'start': start, 'end': end, 'plan_tier': plan_tier}).scalar()


def periods(conn, start, end, granularity, plan_tier, include_incomplete):
    rows = conn.execute(text(f"""
        SELECT DATE_TRUNC(CAST(:grain AS text), signup_date)::date AS period_start,
               SUM(signups)::bigint AS signups, SUM(activated_14d)::bigint AS activated_14d
        FROM gold.fct_activation_daily
        WHERE {_FILTER}
        GROUP BY 1 ORDER BY 1"""),
        _params(start, end, plan_tier, include_incomplete, grain=granularity)).mappings().all()
    return {r['period_start']: dict(r) for r in rows}


def milestone_timing(conn, start, end, plan_tier, include_incomplete):
    """Exact median / 75th percentile days from the integer-day histogram."""
    rows = conn.execute(text(f"""
        WITH h AS (
            SELECT milestone, days_to_milestone, SUM(users) AS n
            FROM gold.fct_activation_milestone_days
            WHERE {_FILTER}
            GROUP BY milestone, days_to_milestone
        ),
        c AS (
            SELECT h.*,
                   SUM(n) OVER (PARTITION BY milestone ORDER BY days_to_milestone) AS cum,
                   SUM(n) OVER (PARTITION BY milestone) AS total
            FROM h
        )
        SELECT milestone, MAX(total)::bigint AS users_reached,
               MIN(days_to_milestone) FILTER (WHERE cum >= 0.50 * total) AS median_days,
               MIN(days_to_milestone) FILTER (WHERE cum >= 0.75 * total) AS p75_days
        FROM c GROUP BY milestone"""),
        _params(start, end, plan_tier, include_incomplete)).mappings().all()
    return {r['milestone']: dict(r) for r in rows}
