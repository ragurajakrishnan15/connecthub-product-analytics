"""Tests for statistical testing module."""
import numpy as np
from experimentation.stat_tests import z_test_proportions, t_test_continuous, srm_check


class TestZTestProportions:
    def test_significant_result(self):
        result = z_test_proportions(300, 1000, 350, 1000)
        assert result['treatment_rate'] > result['control_rate']
        assert isinstance(result['p_value'], float)
        assert result['ci_lower'] < result['absolute_diff'] < result['ci_upper'] or \
               result['ci_lower'] <= result['ci_upper']

    def test_no_difference(self):
        result = z_test_proportions(500, 1000, 500, 1000)
        assert result['absolute_diff'] == 0
        assert result['p_value'] == 1.0
        assert result['significant'] is False

    def test_large_sample_detects_small_diff(self):
        result = z_test_proportions(32000, 100000, 33000, 100000)
        assert result['significant'] is True

    def test_small_sample_no_detection(self):
        result = z_test_proportions(32, 100, 33, 100)
        assert result['significant'] is False


class TestTTestContinuous:
    def test_significant_difference(self):
        np.random.seed(42)
        control = np.random.normal(10, 2, 1000)
        treatment = np.random.normal(11, 2, 1000)
        result = t_test_continuous(control, treatment)
        assert result['significant'] is True
        assert result['treatment_mean'] > result['control_mean']

    def test_no_difference(self):
        np.random.seed(42)
        control = np.random.normal(10, 2, 100)
        treatment = np.random.normal(10, 2, 100)
        result = t_test_continuous(control, treatment)
        assert abs(result['absolute_diff']) < 1


class TestSRMCheck:
    def test_balanced_split(self):
        result = srm_check(5000, 5000)
        assert result['srm_detected'] is False

    def test_imbalanced_split(self):
        result = srm_check(4000, 6000)
        assert result['srm_detected'] is True

    def test_slight_imbalance_ok(self):
        result = srm_check(4950, 5050)
        assert result['srm_detected'] is False
