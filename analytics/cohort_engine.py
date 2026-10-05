"""
Retention Cohort Engine
Generates weekly retention cohort matrix with seaborn heatmap visualization.
"""
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt


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
