"""gold.fct_feature_adoption, gold.fct_feature_usage_monthly, gold.fct_activity_monthly."""
from sqlalchemy import text

SOURCES = ('gold.fct_feature_adoption', 'gold.fct_feature_usage_monthly',
           'gold.fct_activity_monthly')


def curves(conn, features):
    rows = conn.execute(text("""
        SELECT feature_name, days_since_signup, users_adopted, total_users,
               eligible_users, eligible_adopters
        FROM gold.fct_feature_adoption
        WHERE feature_name = ANY(CAST(:features AS text[]))
        ORDER BY feature_name, days_since_signup"""), {'features': list(features)}).mappings().all()
    return [dict(r) for r in rows]


def month_bounds(conn):
    return tuple(conn.execute(text(
        'SELECT MIN(month_start), MAX(month_start) FROM gold.fct_activity_monthly')).one())


def activity(conn, start_month, end_month):
    rows = conn.execute(text("""
        SELECT month_start, active_workspaces, feature_active_workspaces, ai_active_workspaces,
               month_complete
        FROM gold.fct_activity_monthly
        WHERE month_start BETWEEN :start AND :end ORDER BY month_start"""),
        {'start': start_month, 'end': end_month}).mappings().all()
    return [dict(r) for r in rows]


def usage(conn, start_month, end_month, features):
    rows = conn.execute(text("""
        SELECT month_start, feature_name, active_workspaces, active_users
        FROM gold.fct_feature_usage_monthly
        WHERE month_start BETWEEN :start AND :end
          AND feature_name = ANY(CAST(:features AS text[]))
        ORDER BY month_start, feature_name"""),
        {'start': start_month, 'end': end_month, 'features': list(features)}).mappings().all()
    return [dict(r) for r in rows]
