"""Unit tests (no database): date windows, periods, metric formulas, tier
boundaries, and enums that must mirror their sources of truth."""
import typing
from datetime import date

import pytest

from analytics.health_scoring import TIER_BINS, TIER_LABELS
from api import metrics
from api.errors import APIError
from api.params import (PlanTier, RiskTier, Window, buckets, check_granularity, resolve_dates,
                        resolve_months)
from api.services.customer_health import tier_of
from experimentation.decision import decision_code, guardrail_failed

D0, D1 = date(2025, 1, 2), date(2025, 12, 31)


# ---------------------------------------------------------------- date windows
def test_defaults_anchor_on_the_last_loaded_day_not_today():
    w = resolve_dates(None, None, D0, D1, default_days=90)
    assert (w.start, w.end, w.clamped) == (date(2025, 10, 3), D1, False)
    w = resolve_dates(None, None, D0, D1, 90, default_end=date(2025, 12, 17))
    assert (w.start, w.end) == (date(2025, 9, 19), date(2025, 12, 17))


def test_one_sided_ranges_take_the_default_length():
    assert resolve_dates(date(2025, 3, 1), None, D0, D1, 30).end == date(2025, 3, 30)
    assert resolve_dates(None, date(2025, 3, 30), D0, D1, 30).start == date(2025, 3, 1)


def test_partial_overlap_is_clamped_and_flagged():
    w = resolve_dates(date(2024, 12, 1), date(2025, 1, 10), D0, D1, 30)
    assert (w.start, w.end, w.clamped) == (D0, date(2025, 1, 10), True)


def test_a_default_window_longer_than_the_data_is_fitted_without_a_clamp_flag():
    w = resolve_dates(None, None, D0, D1, default_days=365)
    assert (w.start, w.end, w.clamped) == (D0, D1, False)
    assert resolve_dates(None, date(2025, 2, 1), D0, D1, 365).clamped is False
    assert resolve_months(None, None, date(2025, 3, 1), date(2025, 12, 1)).clamped is False


@pytest.mark.parametrize('start,end,message', [
    (date(2025, 5, 2), date(2025, 5, 1), 'is after'),
    (date(2024, 1, 1), date(2025, 6, 1), 'more than 400 days'),
    (date(2026, 1, 1), date(2026, 2, 1), 'outside the available data'),
    (date(2024, 1, 1), date(2024, 2, 1), 'outside the available data'),
])
def test_invalid_ranges(start, end, message):
    with pytest.raises(APIError) as info:
        resolve_dates(start, end, D0, D1, 30)
    assert info.value.slug == 'invalid-range' and message in info.value.detail


def test_month_ranges():
    first, last = date(2025, 1, 1), date(2025, 12, 1)
    w = resolve_months(None, None, first, last)
    assert (w.start, w.end) == (first, last)
    assert resolve_months('2025-11', None, first, last).end == last
    assert resolve_months(None, '2025-03', first, last).start == first   # clamped
    for bad in (('2025-06', '2025-05'), ('2026-01', '2026-03'), ('2021-01', '2025-01')):
        with pytest.raises(APIError):
            resolve_months(*bad, first, last)


def test_day_granularity_is_limited():
    check_granularity('day', Window(date(2025, 1, 1), date(2025, 4, 30)))   # 120 days
    with pytest.raises(APIError, match='limited'):
        check_granularity('day', Window(date(2025, 1, 1), date(2025, 5, 1)))


def test_periods_are_complete_only_when_fully_covered_and_loaded():
    w = Window(date(2025, 12, 3), D1)                  # Wednesday .. Wednesday
    weeks = buckets(w, 'week', data_end=D1)
    assert weeks[0] == (date(2025, 12, 1), date(2025, 12, 3), date(2025, 12, 7), False)
    assert weeks[1] == (date(2025, 12, 8), date(2025, 12, 8), date(2025, 12, 14), True)
    assert weeks[-1] == (date(2025, 12, 29), date(2025, 12, 29), D1, False)   # ends Jan 4
    months = buckets(Window(date(2025, 11, 1), D1), 'month', data_end=D1)
    assert [m[3] for m in months] == [True, True]
    assert buckets(Window(date(2025, 11, 1), date(2025, 12, 15)), 'month', data_end=date(2025, 12, 15))[-1][3] is False


# ---------------------------------------------------------------- metrics
def test_zero_denominators_are_null_not_zero():
    assert metrics.ratio(5, 0) is None and metrics.ratio(0, 0) is None
    assert metrics.ratio(0, 5) == 0.0 and metrics.ratio(1, 3) == 0.333333
    assert metrics.per_thousand(3, 0) is None and metrics.per_thousand(3, 1000) == 3.0
    assert metrics.change(10, 0) == (10, None) and metrics.change(None, 4) == (None, None)
    assert metrics.nps(1, 1, 0) is None and metrics.nps_margin_of_error(1, 1, 0) is None


def test_nps_and_its_margin_of_error():
    assert metrics.nps(50, 20, 100) == 30.0
    assert metrics.nps(0, 100, 100) == -100.0
    # all promoters: no variance
    assert metrics.nps_margin_of_error(10, 0, 10) == 0.0
    # p=.5, d=.3, n=100: var = .8 - .04 = .76 -> 1.96*100*sqrt(.0076) = 17.1
    assert metrics.nps_margin_of_error(50, 30, 100) == 17.1


def test_tier_boundaries_match_health_scoring():
    assert tier_of(0) == 'Critical' and tier_of(40) == 'Critical'
    assert tier_of(40.01) == 'At Risk' and tier_of(55) == 'At Risk'
    assert tier_of(70) == 'Healthy' and tier_of(70.1) == 'Champion' and tier_of(100) == 'Champion'
    assert TIER_BINS == [0, 40, 55, 70, 100]


def test_enums_mirror_their_sources_of_truth():
    assert list(typing.get_args(RiskTier)) == TIER_LABELS
    from quality.expectations import PLAN_TIERS
    assert list(typing.get_args(PlanTier)) == PLAN_TIERS


def test_decision_helpers():
    assert decision_code('REVERT - Revenue guardrail failed') == 'REVERT'
    with pytest.raises(ValueError):
        decision_code('MAYBE - unclear')
    assert guardrail_failed({'significant': True, 'absolute_diff': -0.1}) is True
    assert guardrail_failed({'significant': True, 'absolute_diff': 0.1}) is False
    assert guardrail_failed({'significant': False, 'absolute_diff': -5}) is False
