"""
Shared request-parameter rules (PHASE_4_PLAN.md §4, §8.3).

- Endpoints declare the query parameters they accept; anything else is
  rejected with 400 invalid-parameter instead of being silently ignored.
- Types and enums are validated by FastAPI (422 validation-error).
- Date ranges are validated against the data actually loaded (400
  invalid-range) and default relative to the warehouse's last event date,
  never the wall clock: the dataset is a historical 2025.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from fastapi import Request

from api.errors import APIError

# Accepted values mirror the dbt accepted_values tests (models/schema.yml, serving.yml).
PlanTier = Literal['Free', 'Essentials', 'Professional', 'Enterprise']
Granularity = Literal['day', 'week', 'month']
# Mirrors analytics.health_scoring.TIER_LABELS (a test keeps them equal).
RiskTier = Literal['Critical', 'At Risk', 'Healthy', 'Champion']
DATE_DOC = 'Inclusive, YYYY-MM-DD. Defaults are relative to the last loaded day, not today.'
MONTH_PATTERN = r'^\d{4}-(0[1-9]|1[0-2])$'

MAX_SPAN_DAYS = 400          # longest explicit date range
MAX_DAY_GRANULARITY_DAYS = 120
MAX_SPAN_MONTHS = 36


def allow_query_params(*allowed):
    """Dependency factory: 400 for any query parameter not in `allowed`."""
    allowed_set = set(allowed)

    def dependency(request: Request):
        unknown = sorted(set(request.query_params) - allowed_set)
        if unknown:
            expected = ', '.join(sorted(allowed_set)) or 'none'
            raise APIError('invalid-parameter',
                           f"unknown query parameter(s): {', '.join(unknown)}; accepted: {expected}",
                           errors=[{'loc': ['query', p], 'msg': 'unknown parameter',
                                    'type': 'unknown_parameter'} for p in unknown])
    return dependency


@dataclass(frozen=True)
class Window:
    """An inclusive date range after defaults and clamping to the loaded data."""
    start: date
    end: date
    clamped: bool = False

    @property
    def days(self):
        return (self.end - self.start).days + 1


def resolve_dates(start, end, data_start, data_end, default_days, default_end=None,
                  param_names=('start', 'end')):
    """Effective [start, end] for a request.

    Defaults: `end` = default_end (normally the last event date), `start` =
    end - default_days + 1. Explicit values must satisfy start <= end and span
    at most MAX_SPAN_DAYS, and must overlap the loaded data; a partial overlap
    is clamped (Window.clamped) and reported by the caller.
    """
    default_end = default_end or data_end
    explicit_start, explicit_end = start is not None, end is not None
    if end is None:
        end = default_end if start is None else min(data_end, start + timedelta(days=default_days - 1))
    if start is None:
        start = end - timedelta(days=default_days - 1)
    s_name, e_name = param_names
    if start > end:
        raise APIError('invalid-range', f'{s_name} ({start}) is after {e_name} ({end})')
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise APIError('invalid-range', f'the range spans more than {MAX_SPAN_DAYS} days')
    if end < data_start or start > data_end:
        raise APIError('invalid-range', f'{start}..{end} is outside the available data '
                                        f'({data_start}..{data_end})')
    clamped_start, clamped_end = max(start, data_start), min(end, data_end)
    # Only a bound the caller supplied counts as "clamped"; defaults fit silently.
    return Window(clamped_start, clamped_end,
                  clamped=(explicit_start and clamped_start != start)
                  or (explicit_end and clamped_end != end))


def parse_month(value):
    return None if value is None else date(int(value[:4]), int(value[5:7]), 1)


def add_months(d, months):
    index = d.year * 12 + d.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def resolve_months(start_month, end_month, first_month, last_month, default_months=12):
    """Effective [start_month, end_month] (first days of months), like resolve_dates."""
    start, end = parse_month(start_month), parse_month(end_month)
    explicit_start, explicit_end = start is not None, end is not None
    if end is None:
        end = last_month if start is None else min(last_month, add_months(start, default_months - 1))
    if start is None:
        start = add_months(end, -(default_months - 1))
    if start > end:
        raise APIError('invalid-range', f'start_month ({start:%Y-%m}) is after end_month '
                                        f'({end:%Y-%m})')
    span = (end.year - start.year) * 12 + end.month - start.month + 1
    if span > MAX_SPAN_MONTHS:
        raise APIError('invalid-range', f'the range spans more than {MAX_SPAN_MONTHS} months')
    if end < first_month or start > last_month:
        raise APIError('invalid-range', f'{start:%Y-%m}..{end:%Y-%m} is outside the available '
                                        f'months ({first_month:%Y-%m}..{last_month:%Y-%m})')
    clamped_start, clamped_end = max(start, first_month), min(end, last_month)
    return Window(clamped_start, clamped_end,
                  clamped=(explicit_start and clamped_start != start)
                  or (explicit_end and clamped_end != end))


def month_window_dates(window, data_end):
    """A month Window as dates: first day of the first month .. last day of the last
    month, capped at the last loaded day (for meta.effective_range)."""
    return Window(window.start, min(bucket_end(window.end, 'month'), data_end), window.clamped)


def check_granularity(granularity, window):
    if granularity == 'day' and window.days > MAX_DAY_GRANULARITY_DAYS:
        raise APIError('invalid-parameter',
                       f'granularity=day is limited to ranges of {MAX_DAY_GRANULARITY_DAYS} days '
                       f'(this range has {window.days}); use week or month')


def bucket_start(d, granularity):
    if granularity == 'week':
        return d - timedelta(days=d.weekday())
    if granularity == 'month':
        return d.replace(day=1)
    return d


def bucket_end(start, granularity):
    if granularity == 'week':
        return start + timedelta(days=6)
    if granularity == 'month':
        return add_months(start, 1) - timedelta(days=1)
    return start


def buckets(window, granularity, data_end):
    """Every period overlapping the window: (nominal_start, covered_start, covered_end, complete).

    A period is complete when the window covers all of it and it ends on or
    before the last loaded event date.
    """
    out = []
    start = bucket_start(window.start, granularity)
    while start <= window.end:
        end = bucket_end(start, granularity)
        covered = (max(start, window.start), min(end, window.end))
        out.append((start, covered[0], covered[1],
                    covered == (start, end) and end <= data_end))
        start = end + timedelta(days=1)
    return out
