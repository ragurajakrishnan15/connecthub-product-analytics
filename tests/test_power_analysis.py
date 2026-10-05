"""Tests for power analysis module."""
from experimentation.power_analysis import (
    required_sample_size,
    required_sample_size_continuous,
    experiment_duration
)


class TestSampleSize:
    def test_basic_calculation(self):
        n = required_sample_size(baseline_rate=0.32, mde=0.05)
        assert 10000 < n < 20000

    def test_higher_power_needs_more(self):
        n_80 = required_sample_size(baseline_rate=0.32, mde=0.05, power=0.80)
        n_90 = required_sample_size(baseline_rate=0.32, mde=0.05, power=0.90)
        assert n_90 > n_80

    def test_smaller_mde_needs_more(self):
        n_5pct = required_sample_size(baseline_rate=0.32, mde=0.05)
        n_2pct = required_sample_size(baseline_rate=0.32, mde=0.02)
        assert n_2pct > n_5pct

    def test_returns_integer(self):
        n = required_sample_size(baseline_rate=0.50, mde=0.10)
        assert isinstance(n, int)
        assert n > 0


class TestContinuousSampleSize:
    def test_basic(self):
        n = required_sample_size_continuous(
            baseline_mean=12.0, baseline_std=4.0, mde=0.10
        )
        assert n > 0
        assert isinstance(n, int)


class TestDuration:
    def test_basic_duration(self):
        days = experiment_duration(10000, daily_traffic=5000)
        assert days == 4  # 20000 total / 5000 per day

    def test_partial_traffic(self):
        days = experiment_duration(10000, daily_traffic=5000, traffic_pct=0.5)
        assert days == 8
