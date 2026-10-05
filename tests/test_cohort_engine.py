"""Tests for cohort engine module."""
import pandas as pd

from analytics.cohort_engine import generate_retention_from_parquet, retention_table


def frames():
    """Two cohorts: week of Mon 2025-01-06 (u1, u2) and week of 2025-01-13 (u3).

    u4 never became active, so it has no cohort.
    """
    users = pd.DataFrame({
        'user_id': ['u1', 'u2', 'u3', 'u4'],
        'first_active_date': pd.to_datetime(['2025-01-06', '2025-01-08', '2025-01-13', None]),
    })
    events = pd.DataFrame({
        'user_id': ['u1', 'u1', 'u1', 'u1', 'u2', 'u2', 'u3', 'u3', 'u4'],
        'event_date': pd.to_datetime([
            '2025-01-06', '2025-01-12',  # week 0 (Sunday still belongs to week 0)
            '2025-01-14',                # week 1
            '2025-01-29',                # week 3
            '2025-01-08',                # week 0
            '2025-01-01',                # before the cohort week: ignored
            '2025-01-13', '2025-01-20',  # weeks 0 and 1 of the second cohort
            '2025-01-06',                # no cohort: ignored
        ]),
    })
    return users, events


def cell(table, cohort, week):
    row = table[(table['cohort_week'] == pd.Timestamp(cohort))
                & (table['weeks_since_signup'] == week)]
    return row.iloc[0] if len(row) else None


class TestRetentionMatrix:
    def test_retention_rate_bounds(self, sample_data):
        """Retention rate should always be between 0 and 100."""
        users = pd.read_parquet(sample_data / 'users.parquet')
        events = pd.read_parquet(sample_data / 'events.parquet')
        table = retention_table(users, events)
        assert len(table) > 0
        assert table['retention_rate'].between(0, 100).all()

    def test_week_0_is_100_percent(self, sample_data):
        """Cohorts are first-active weeks, so every cohort is fully active in week 0."""
        users = pd.read_parquet(sample_data / 'users.parquet')
        events = pd.read_parquet(sample_data / 'events.parquet')
        table = retention_table(users, events)
        week_0 = table[table['weeks_since_signup'] == 0]
        assert (week_0['retention_rate'] == 100.0).all()
        assert week_0['cohort_size'].sum() == users['first_active_date'].notna().sum()

    def test_monotonic_decrease(self, sample_data):
        """Pooled across cohorts, retention falls from week 1 to week 12."""
        users = pd.read_parquet(sample_data / 'users.parquet')
        events = pd.read_parquet(sample_data / 'events.parquet')
        table = retention_table(users, events)
        pooled = table.groupby('weeks_since_signup')[['active_users', 'cohort_size']].sum()
        rate = pooled['active_users'] / pooled['cohort_size']
        assert rate[1] < rate[0]
        assert rate[12] < rate[4] < rate[1]

    def test_exact_cells(self):
        table = retention_table(*frames())
        assert cell(table, '2025-01-06', 0)[['active_users', 'cohort_size', 'retention_rate']] \
            .tolist() == [2, 2, 100.0]
        assert cell(table, '2025-01-06', 1)['retention_rate'] == 50.0
        assert cell(table, '2025-01-06', 2) is None  # nobody active in week 2
        assert cell(table, '2025-01-06', 3)['retention_rate'] == 50.0
        assert cell(table, '2025-01-13', 0)['cohort_size'] == 1
        assert cell(table, '2025-01-13', 1)['retention_rate'] == 100.0
        assert len(table) == 5

    def test_activity_before_cohort_and_users_without_cohort_are_ignored(self):
        table = retention_table(*frames())
        assert (table['weeks_since_signup'] >= 0).all()
        assert table.groupby('cohort_week')['cohort_size'].first().sum() == 3

    def test_num_weeks_limits_the_horizon(self):
        table = retention_table(*frames(), num_weeks=2)
        assert table['weeks_since_signup'].max() == 1
        assert cell(table, '2025-01-06', 3) is None

    def test_parquet_entry_point_returns_matrix(self, sample_data):
        import matplotlib.pyplot as plt
        fig, matrix = generate_retention_from_parquet(
            sample_data / 'users.parquet', sample_data / 'events.parquet', num_weeks=4)
        plt.close(fig)
        assert list(matrix.columns) == [0, 1, 2, 3, 4]
        assert (matrix[0] == 100.0).all()
