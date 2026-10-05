"""
Retention cohorts: pure functions with no plotting imports.

Shared by analytics/cohort_engine.py (CLI and heatmaps, which re-exports these
names) and the analytics API, whose image has no matplotlib/seaborn.

Cell rule (docs/metric-definitions.md): gold.fct_retention_cohorts has no row
for a week with zero active users. A missing cell whose week has ended counts
as 0% retention; a cell whose week has not ended by the as-of date is unknown
and dropped by as_of().
"""
import os

import pandas as pd

SUMMARY_WEEKS = (1, 4, 8, 12)


def retention_table(users, events, num_weeks=12):
    """Weekly retention cells from user and event frames; mirrors gold.fct_retention_cohorts.

    A user's cohort is the (Monday-start) week of their first_active_date; users
    without one are excluded. Week N retention is the share of the cohort with
    any event in the Nth week after the cohort week.
    """
    users = users.dropna(subset=['first_active_date'])
    cohorts = pd.DataFrame({
        'user_id': users['user_id'],
        'cohort_week': _week_start(users['first_active_date']),
    })
    activity = pd.DataFrame({
        'user_id': events['user_id'],
        'activity_week': _week_start(events['event_date']),
    }).drop_duplicates()

    merged = activity.merge(cohorts, on='user_id')
    merged['weeks_since_signup'] = (merged['activity_week'] - merged['cohort_week']).dt.days // 7
    merged = merged[merged['weeks_since_signup'].between(0, num_weeks)]

    retention = merged.groupby(['cohort_week', 'weeks_since_signup'])['user_id'] \
        .nunique().rename('active_users').reset_index()
    cohort_sizes = cohorts.groupby('cohort_week')['user_id'].nunique().rename('cohort_size')
    retention = retention.merge(cohort_sizes, on='cohort_week')
    retention['retention_rate'] = (
        retention['active_users'] / retention['cohort_size'] * 100
    ).round(2)
    return retention.sort_values(['cohort_week', 'weeks_since_signup']).reset_index(drop=True)


def _week_start(dates):
    """Monday of each date's week (PostgreSQL DATE_TRUNC('week'))."""
    dates = pd.to_datetime(dates).dt.normalize()
    return dates - pd.to_timedelta(dates.dt.weekday, unit='D')


def as_of(table, date):
    """Cells that are complete on `date`: the cohort week and week N have ended."""
    date = pd.Timestamp(date)
    cohort = pd.to_datetime(table['cohort_week'])
    week_end = cohort + pd.to_timedelta(7 * (table['weeks_since_signup'] + 1) - 1, unit='D')
    return table[week_end <= date].reset_index(drop=True)


def pooled_curve(table, date, weeks=range(13)):
    """Pooled week-N retention with the counts behind it.

    For each week N: the cohorts whose week N has ended by `date` (eligible),
    their combined size, and their combined active users in week N. A missing
    cell of an eligible cohort contributes 0 active users. Returns one dict per
    week: {week, cohorts, cohort_users, active_users, retention_pct}, with
    retention_pct None when no cohort is eligible.
    """
    date = pd.Timestamp(date)
    sizes = table[table['weeks_since_signup'] == 0].set_index('cohort_week')['cohort_size']
    sizes.index = pd.to_datetime(sizes.index)
    cohort_weeks = pd.to_datetime(table['cohort_week'])
    out = []
    for week in weeks:
        eligible = sizes[sizes.index + pd.Timedelta(days=7 * (week + 1) - 1) <= date]
        cells = table[(table['weeks_since_signup'] == week) & cohort_weeks.isin(eligible.index)]
        users, active = int(eligible.sum()), int(cells['active_users'].sum())
        out.append({'week': week, 'cohorts': int(len(eligible)), 'cohort_users': users,
                    'active_users': active,
                    'retention_pct': round(100 * active / users, 2) if users else None})
    return out


def summarize(table, date, weeks=SUMMARY_WEEKS):
    """Pooled week-N retention over cohorts whose week N is complete on `date`.

    `weeks` selects which week numbers to pool (default: 1, 4, 8, 12); a week
    with no complete cohort is omitted from the result.
    """
    date = pd.Timestamp(date)
    sizes = table[table['weeks_since_signup'] == 0].set_index('cohort_week')['cohort_size']
    pooled = {f"week_{p['week']}": p['retention_pct'] for p in pooled_curve(table, date, weeks)
              if p['retention_pct'] is not None}
    return {
        'as_of': str(date.date()),
        'cohorts': int(len(sizes)),
        'users_in_cohorts': int(sizes.sum()),
        'pooled_retention_pct': pooled,
    }


def load_retention(source='warehouse', data_dir='data', num_weeks=12, engine=None):
    """All retention cells (cohort_week, weeks_since_signup, active_users, cohort_size, rate)."""
    if source == 'parquet':
        users = pd.read_parquet(os.path.join(data_dir, 'users.parquet'),
                                columns=['user_id', 'first_active_date'])
        events = pd.read_parquet(os.path.join(data_dir, 'events.parquet'),
                                 columns=['user_id', 'event_date'])
        return retention_table(users, events, num_weeks)
    if engine is None:
        from pipeline.config import create_engine
        engine = create_engine()
    return pd.read_sql(
        'SELECT cohort_week, weeks_since_signup, active_users, cohort_size, retention_rate '
        'FROM gold.fct_retention_cohorts WHERE weeks_since_signup <= %(w)s '
        'ORDER BY cohort_week, weeks_since_signup', engine, params={'w': num_weeks})
