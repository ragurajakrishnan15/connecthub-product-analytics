"""gold.fct_nps_daily (response date x billed plan, additive)."""
from sqlalchemy import text

SOURCES = ('gold.fct_nps_daily',)
_COUNTS = """COALESCE(SUM(responses), 0)::bigint AS responses,
       COALESCE(SUM(promoters), 0)::bigint AS promoters,
       COALESCE(SUM(passives), 0)::bigint AS passives,
       COALESCE(SUM(detractors), 0)::bigint AS detractors"""
_FILTER = """response_date BETWEEN :start AND :end
  AND (CAST(:plan_tier AS text) IS NULL OR plan_tier = :plan_tier)"""


def totals(conn, start, end, plan_tier):
    return dict(conn.execute(text(f'SELECT {_COUNTS} FROM gold.fct_nps_daily WHERE {_FILTER}'),
                             {'start': start, 'end': end, 'plan_tier': plan_tier}).mappings().one())


def by_plan(conn, start, end, plan_tier):
    rows = conn.execute(text(f"""
        SELECT plan_tier, {_COUNTS} FROM gold.fct_nps_daily WHERE {_FILTER}
        GROUP BY plan_tier ORDER BY plan_tier"""),
        {'start': start, 'end': end, 'plan_tier': plan_tier}).mappings().all()
    return [dict(r) for r in rows]


def periods(conn, start, end, granularity, plan_tier):
    rows = conn.execute(text(f"""
        SELECT DATE_TRUNC(CAST(:grain AS text), response_date)::date AS period_start, {_COUNTS}
        FROM gold.fct_nps_daily WHERE {_FILTER}
        GROUP BY 1 ORDER BY 1"""),
        {'start': start, 'end': end, 'plan_tier': plan_tier, 'grain': granularity}
    ).mappings().all()
    return {r['period_start']: dict(r) for r in rows}
