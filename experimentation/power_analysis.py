"""
Power Analysis & Sample Size Calculator
Determines required sample size for A/B tests.
"""
import numpy as np
from scipy import stats


def required_sample_size(
    baseline_rate: float,
    mde: float,
    alpha: float = 0.05,
    power: float = 0.80,
    two_sided: bool = True
) -> int:
    """
    Calculate required sample size per variant for a proportion test.

    Args:
        baseline_rate: Current conversion rate (e.g., 0.32 for 32%)
        mde: Minimum detectable effect as relative change (e.g., 0.05 for 5% lift)
        alpha: Significance level (default 0.05)
        power: Statistical power (default 0.80)
        two_sided: Whether to use two-sided test (default True)

    Returns:
        Required sample size per variant
    """
    p1 = baseline_rate
    p2 = baseline_rate * (1 + mde)
    pooled_p = (p1 + p2) / 2

    z_alpha = stats.norm.ppf(1 - alpha / (2 if two_sided else 1))
    z_beta = stats.norm.ppf(power)

    n = (
        (z_alpha * np.sqrt(2 * pooled_p * (1 - pooled_p))
         + z_beta * np.sqrt(p1 * (1 - p1) + p2 * (1 - p2)))
        / (p2 - p1)
    ) ** 2
    return int(np.ceil(n))


def required_sample_size_continuous(
    baseline_mean: float,
    baseline_std: float,
    mde: float,
    alpha: float = 0.05,
    power: float = 0.80
) -> int:
    """Sample size for continuous metric experiments (e.g., session duration)."""
    delta = baseline_mean * mde
    z_alpha = stats.norm.ppf(1 - alpha / 2)
    z_beta = stats.norm.ppf(power)

    n = (2 * ((z_alpha + z_beta) * baseline_std / delta) ** 2)
    return int(np.ceil(n))


def experiment_duration(
    sample_size_per_variant: int,
    daily_traffic: int,
    num_variants: int = 2,
    traffic_pct: float = 1.0
) -> int:
    """Estimate how many days the experiment needs to run."""
    total_needed = sample_size_per_variant * num_variants
    daily_eligible = daily_traffic * traffic_pct
    return int(np.ceil(total_needed / daily_eligible))


if __name__ == '__main__':
    # Example: detect a 5% lift in activation rate (baseline 32%)
    n = required_sample_size(baseline_rate=0.32, mde=0.05)
    print(f"Required sample size per variant: {n:,}")
    print(f"Total users needed: {n * 2:,}")

    days = experiment_duration(n, daily_traffic=5000)
    print(f"Estimated duration at 5K daily users: {days} days")
