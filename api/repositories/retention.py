"""gold.fct_retention_cohorts (cohort week x weeks since first activity, 0..12)."""
import pandas as pd
from sqlalchemy import text

SOURCES = ('gold.fct_retention_cohorts',)


def cells(conn, max_week=12):
    """All retention cells as the DataFrame analytics.retention expects."""
    df = pd.read_sql(text(
        'SELECT cohort_week, weeks_since_signup, active_users, cohort_size, retention_rate '
        'FROM gold.fct_retention_cohorts WHERE weeks_since_signup <= :w '
        'ORDER BY cohort_week, weeks_since_signup'), conn, params={'w': max_week})
    df['cohort_week'] = pd.to_datetime(df['cohort_week'])
    return df
