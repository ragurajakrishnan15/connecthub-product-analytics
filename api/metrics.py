"""
Derived metrics computed by the API from additive warehouse counts.

Each formula is defined in docs/metric-definitions.md; this is the only place
the API computes ratios. Rules: a zero (or missing) denominator gives None
(JSON null), never 0; rates are fractions in [0, 1]; ratios of sums, never
averages of averages.
"""
from math import sqrt


def ratio(numerator, denominator, digits=6):
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, digits)


def per_thousand(numerator, denominator):
    value = ratio(numerator, denominator, digits=9)
    return None if value is None else round(1000 * value, 3)


def change(current, previous):
    """(absolute, relative) change; relative is None when previous is 0 or missing."""
    if current is None or previous is None:
        return None, None
    return round(current - previous, 6), ratio(current - previous, previous)


def nps(promoters, detractors, responses):
    """Net Promoter Score, -100..100: 100 * (promoters - detractors) / responses."""
    if not responses:
        return None
    return round(100 * (promoters - detractors) / responses, 1)


def nps_margin_of_error(promoters, detractors, responses):
    """95% margin of error of the NPS, in NPS points.

    Each response scores +1 / 0 / -1; the variance of that score is
    (P + D)/N - ((P - D)/N)^2, and the margin is 1.96 * 100 * sqrt(variance / N).
    """
    if not responses:
        return None
    p, d = promoters / responses, detractors / responses
    variance = max(p + d - (p - d) ** 2, 0.0)
    return round(1.96 * 100 * sqrt(variance / responses), 1)
