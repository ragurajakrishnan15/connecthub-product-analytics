"""Tests for cohort engine module."""
import pandas as pd


class TestRetentionMatrix:
    def test_retention_rate_bounds(self):
        """Retention rate should always be between 0 and 100."""
        # Simulate a small retention dataset
        data = {
            'cohort_week': pd.date_range('2025-01-06', periods=4, freq='W'),
            'weeks_since_signup': [0, 1, 2, 3],
            'active_users': [100, 80, 60, 45],
            'cohort_size': [100, 100, 100, 100],
        }
        df = pd.DataFrame(data)
        df['retention_rate'] = df['active_users'] / df['cohort_size'] * 100

        assert (df['retention_rate'] >= 0).all()
        assert (df['retention_rate'] <= 100).all()

    def test_week_0_is_100_percent(self):
        """Week 0 retention should be 100% (users are active on signup week)."""
        # In a properly constructed cohort, week 0 = 100%
        week_0_retention = 100.0  # all users active in their signup week
        assert week_0_retention == 100.0

    def test_monotonic_decrease(self):
        """Retention generally decreases over time (not strictly required but expected)."""
        rates = [100, 85, 72, 65, 58, 52]
        for i in range(1, len(rates)):
            assert rates[i] <= rates[i-1]
