"""
Statistical Significance Testing
Z-test for proportions and Welch's t-test for continuous metrics.
"""
import numpy as np
from scipy import stats

# Results are returned as native Python types (float/bool/int) rather than
# numpy scalars so they compare with `is True`/`is False` and serialize to JSON.


def z_test_proportions(control_conversions, control_total,
                       treatment_conversions, treatment_total,
                       alpha=0.05):
    """Two-proportion Z-test for conversion rate experiments."""
    p_c = control_conversions / control_total
    p_t = treatment_conversions / treatment_total
    p_pool = (control_conversions + treatment_conversions) / \
             (control_total + treatment_total)

    se = np.sqrt(p_pool * (1 - p_pool) * (1/control_total + 1/treatment_total))
    z_stat = (p_t - p_c) / se
    p_value = 2 * (1 - stats.norm.cdf(abs(z_stat)))

    ci_95 = 1.96 * np.sqrt(
        p_c * (1 - p_c) / control_total +
        p_t * (1 - p_t) / treatment_total
    )
    lift = (p_t - p_c) / p_c if p_c > 0 else 0

    return {
        'control_rate': float(round(p_c, 4)),
        'treatment_rate': float(round(p_t, 4)),
        'absolute_diff': float(round(p_t - p_c, 4)),
        'relative_lift': float(round(lift, 4)),
        'z_statistic': float(round(z_stat, 4)),
        'p_value': float(round(p_value, 6)),
        'ci_lower': float(round((p_t - p_c) - ci_95, 4)),
        'ci_upper': float(round((p_t - p_c) + ci_95, 4)),
        'significant': bool(p_value < alpha)
    }


def t_test_continuous(control_values, treatment_values, alpha=0.05):
    """Welch's t-test for continuous metrics (e.g., session duration)."""
    control_values = np.array(control_values)
    treatment_values = np.array(treatment_values)

    t_stat, p_value = stats.ttest_ind(
        treatment_values, control_values, equal_var=False
    )

    diff = np.mean(treatment_values) - np.mean(control_values)
    se = np.sqrt(
        np.var(control_values, ddof=1) / len(control_values) +
        np.var(treatment_values, ddof=1) / len(treatment_values)
    )

    return {
        'control_mean': float(round(np.mean(control_values), 4)),
        'treatment_mean': float(round(np.mean(treatment_values), 4)),
        'absolute_diff': float(round(diff, 4)),
        'relative_lift': float(round(diff / np.mean(control_values), 4)) if np.mean(control_values) != 0 else 0.0,
        't_statistic': float(round(t_stat, 4)),
        'p_value': float(round(p_value, 6)),
        'ci_lower': float(round(diff - 1.96 * se, 4)),
        'ci_upper': float(round(diff + 1.96 * se, 4)),
        'significant': bool(p_value < alpha)
    }


def srm_check(control_count, treatment_count, expected_ratio=0.5):
    """Sample Ratio Mismatch check using chi-squared test."""
    total = control_count + treatment_count
    expected_control = total * expected_ratio
    expected_treatment = total * (1 - expected_ratio)

    chi2 = ((control_count - expected_control)**2 / expected_control +
            (treatment_count - expected_treatment)**2 / expected_treatment)
    p_value = 1 - stats.chi2.cdf(chi2, df=1)

    return {
        'control_count': control_count,
        'treatment_count': treatment_count,
        'actual_ratio': float(round(control_count / total, 4)),
        'chi2_statistic': float(round(chi2, 4)),
        'p_value': float(round(p_value, 6)),
        'srm_detected': bool(p_value < 0.01)
    }
