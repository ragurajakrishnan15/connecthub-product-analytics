"""gold.fct_revenue_monthly (month x billed plan, additive)."""
from sqlalchemy import text

SOURCES = ('gold.fct_revenue_monthly',)
MONEY = ('mrr_usd', 'previous_mrr_usd', 'new_mrr_usd', 'expansion_mrr_usd',
         'reactivation_mrr_usd', 'contraction_mrr_usd', 'churned_mrr_usd')


def month_bounds(conn):
    return tuple(conn.execute(text(
        'SELECT MIN(month_start), MAX(month_start) FROM gold.fct_revenue_monthly')).one())


def months(conn, start_month, end_month, plan_tier):
    rows = conn.execute(text("""
        SELECT month_start, plan_tier, paying_workspaces, billed_seats, month_complete,
               mrr_usd, previous_mrr_usd, new_mrr_usd, expansion_mrr_usd, reactivation_mrr_usd,
               contraction_mrr_usd, churned_mrr_usd
        FROM gold.fct_revenue_monthly
        WHERE month_start BETWEEN :start AND :end
          AND (CAST(:plan_tier AS text) IS NULL OR plan_tier = :plan_tier)
        ORDER BY month_start, plan_tier"""),
        {'start': start_month, 'end': end_month, 'plan_tier': plan_tier}).mappings().all()
    return [{**r, **{k: float(r[k]) for k in MONEY}} for r in rows]
