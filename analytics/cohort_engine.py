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


def generate_retention_from_parquet(users_path, events_path, num_weeks=12):
    """Generate retention matrix from local parquet files (for local dev)."""
    users = pd.read_parquet(users_path)
    events = pd.read_parquet(events_path)

    # Assign cohort week
    users['cohort_week'] = pd.to_datetime(users['signup_date']).dt.to_period('W').dt.start_time
    events['activity_week'] = pd.to_datetime(events['event_date']).dt.to_period('W').dt.start_time

    # Merge
    merged = events[['user_id', 'activity_week']].drop_duplicates().merge(
        users[['user_id', 'cohort_week']], on='user_id'
    )
    merged['weeks_since_signup'] = (
        (merged['activity_week'] - merged['cohort_week']).dt.days / 7
    ).astype(int)

    # Compute retention
    cohort_sizes = users.groupby('cohort_week')['user_id'].nunique().reset_index()
    cohort_sizes.columns = ['cohort_week', 'cohort_size']

    retention = merged[merged['weeks_since_signup'].between(0, num_weeks)] \
        .groupby(['cohort_week', 'weeks_since_signup'])['user_id'] \
        .nunique().reset_index()
    retention.columns = ['cohort_week', 'weeks_since_signup', 'active_users']

    retention = retention.merge(cohort_sizes, on='cohort_week')
    retention['retention_rate'] = (
        retention['active_users'] / retention['cohort_size'] * 100
    ).round(2)

    # Pivot and plot
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
