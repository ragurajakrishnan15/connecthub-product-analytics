"""
Retention Cohort Engine
Generates weekly retention cohort matrix with seaborn heatmap visualization.

CLI (retention as known on a date; only weeks complete by then are counted):
    python -m analytics.cohort_engine --date 2025-12-31 [--weeks 12]
        [--source warehouse|parquet] [--data-dir data] [--output cells.csv] [--json]
Exit codes: 0 ok, 1 no data or error, 2 usage error.
"""
import argparse
import json
import os
import sys

import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

SUMMARY_WEEKS = (1, 4, 8, 12)


def generate_retention_matrix(conn, num_weeks=12):
    """Generate retention cohort matrix from gold tables."""
    query = """
    SELECT
        cohort_week,
        weeks_since_signup,
        retention_rate
    FROM gold.fct_retention_cohorts
    WHERE weeks_since_signup <= %(num_weeks)s
    ORDER BY cohort_week, weeks_since_signup
    """
    df = pd.read_sql(query, conn, params={'num_weeks': num_weeks})

    # Pivot into matrix form
    matrix = df.pivot(
        index='cohort_week',
        columns='weeks_since_signup',
        values='retention_rate'
    )

    fig, ax = plt.subplots(figsize=(16, 10))
    sns.heatmap(
        matrix,
        annot=True, fmt='.1f', cmap='Blues',
        linewidths=0.5, linecolor='white',
        cbar_kws={'label': 'Retention Rate (%)'},
        ax=ax
    )
    ax.set_title('Weekly Retention Cohort Matrix', fontsize=16, pad=20)
    ax.set_xlabel('Weeks Since Signup')
    ax.set_ylabel('Cohort Week')
    plt.tight_layout()
    return fig, matrix


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

    retention = merged.groupby(['cohort_week', 'weeks_since_signup'])['user_id']         .nunique().rename('active_users').reset_index()
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


def generate_retention_from_parquet(users_path, events_path, num_weeks=12):
    """Generate retention matrix from local parquet files (for local dev)."""
    users = pd.read_parquet(users_path, columns=['user_id', 'first_active_date'])
    events = pd.read_parquet(events_path, columns=['user_id', 'event_date'])
    retention = retention_table(users, events, num_weeks)

    matrix = retention.pivot(
        index='cohort_week', columns='weeks_since_signup', values='retention_rate'
    )

    fig, ax = plt.subplots(figsize=(16, 10))
    sns.heatmap(
        matrix, annot=True, fmt='.1f', cmap='Blues',
        linewidths=0.5, linecolor='white',
        cbar_kws={'label': 'Retention Rate (%)'},
        ax=ax
    )
    ax.set_title('Weekly Retention Cohort Matrix', fontsize=16, pad=20)
    ax.set_xlabel('Weeks Since Signup')
    ax.set_ylabel('Cohort Week')
    plt.tight_layout()
    return fig, matrix


def as_of(table, date):
    """Cells that are complete on `date`: the cohort week and week N have ended."""
    date = pd.Timestamp(date)
    cohort = pd.to_datetime(table['cohort_week'])
    week_end = cohort + pd.to_timedelta(7 * (table['weeks_since_signup'] + 1) - 1, unit='D')
    return table[week_end <= date].reset_index(drop=True)


def summarize(table, date):
    """Pooled week-N retention over cohorts whose week N is complete on `date`."""
    date = pd.Timestamp(date)
    sizes = table[table['weeks_since_signup'] == 0].set_index('cohort_week')['cohort_size']
    sizes.index = pd.to_datetime(sizes.index)
    pooled = {}
    for week in SUMMARY_WEEKS:
        eligible = sizes[sizes.index + pd.Timedelta(days=7 * (week + 1) - 1) <= date]
        if eligible.empty:
            continue
        cells = table[(table['weeks_since_signup'] == week)
                      & pd.to_datetime(table['cohort_week']).isin(eligible.index)]
        pooled[f'week_{week}'] = round(100 * cells['active_users'].sum() / eligible.sum(), 2)
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m analytics.cohort_engine',
                                     description='Weekly retention cohorts as of a date.')
    parser.add_argument('--date', required=True, help='As-of date, YYYY-MM-DD')
    parser.add_argument('--weeks', type=int, default=12)
    parser.add_argument('--source', choices=['warehouse', 'parquet'], default='warehouse')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--output', help='Write the as-of retention cells to this CSV')
    parser.add_argument('--json', action='store_true', help='Print the summary as JSON')
    args = parser.parse_args(argv)
    try:
        date = pd.Timestamp(args.date)
    except ValueError:
        parser.error(f'--date must be YYYY-MM-DD, got {args.date!r}')

    try:
        cells = as_of(load_retention(args.source, args.data_dir, args.weeks), date)
    except Exception as exc:
        print(f'error: could not load retention from {args.source}: {exc}', file=sys.stderr)
        return 1
    if cells.empty:
        print(f'error: no complete retention cohorts as of {date.date()}', file=sys.stderr)
        return 1
    summary = summarize(cells, date)
    if args.output:
        cells.to_csv(args.output, index=False)
        summary['output'] = args.output
    if args.json:
        print(json.dumps(summary))
    else:
        rates = ', '.join(f"{k.replace('_', ' ')}: {v}%"
                          for k, v in summary['pooled_retention_pct'].items())
        print(f"Retention as of {summary['as_of']}: {summary['cohorts']} cohorts, "
              f"{summary['users_in_cohorts']:,} users. Pooled {rates}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
