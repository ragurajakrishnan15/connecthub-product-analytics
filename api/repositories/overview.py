"""Month-level KPI inputs for /api/overview (the other KPIs reuse the domain repositories)."""
from sqlalchemy import text

SOURCES = ('gold.fct_revenue_monthly', 'gold.fct_daily_active_users',
           'gold.fct_retention_cohorts', 'gold.fct_activity_monthly',
           'gold.fct_activation_daily', 'gold.fct_nps_daily', 'gold.fct_agent_performance_daily',
           'analytics.workspace_health_scores', 'analytics.experiment_results')


def complete_revenue_months(conn, n=2):
    """The last n complete months: (month_start, mrr_usd), newest first."""
    rows = conn.execute(text("""
        SELECT month_start, SUM(mrr_usd)::float AS mrr_usd
        FROM gold.fct_revenue_monthly
        GROUP BY month_start
        HAVING BOOL_AND(month_complete)
        ORDER BY month_start DESC
        LIMIT :n"""), {'n': n}).mappings().all()
    return [dict(r) for r in rows]


def complete_activity_months(conn, n=2):
    rows = conn.execute(text("""
        SELECT month_start, ai_active_workspaces, feature_active_workspaces
        FROM gold.fct_activity_monthly
        WHERE month_complete
        ORDER BY month_start DESC
        LIMIT :n"""), {'n': n}).mappings().all()
    return [dict(r) for r in rows]
