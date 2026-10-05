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
import sys

import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

# The pure retention functions live in analytics/retention.py (no plotting
# imports, so the API can use them); re-exported here for existing callers.
from analytics.retention import (  # noqa: F401
    SUMMARY_WEEKS, _week_start, as_of, load_retention, retention_table, summarize,
)


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
