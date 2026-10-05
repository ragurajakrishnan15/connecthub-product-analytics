"""Weekly retention: pooled summary and cohort matrix (docs/metric-definitions.md#retention).

Business logic is analytics/retention.py (as_of, pooled_curve): a cell counts
only once its week has ended by as_of; an ended week with no row in gold
(nobody active) counts as 0; a week that has not ended is unknown (null).
"""
from datetime import timedelta

import pandas as pd

from analytics.retention import as_of as complete_as_of
from analytics.retention import pooled_curve
from api import metrics
from api.errors import APIError
from api.params import bucket_start
from api.repositories import retention as repo
from api.schemas.retention import (CohortCell, CohortMatrix, CohortMonth, CohortRow, Headline,
                                   PooledWeek, RetentionData)
from api.services import context

MAX_WEEK = 12
MAX_COHORTS = 53
DEFAULT_COHORTS = 26
CAVEAT = ('Cohorts are users grouped by the Monday-start week of their first product '
          'activity; retention has no plan-tier breakdown.')


def _resolve_as_of(ctx, as_of):
    as_of = as_of or ctx.data_end
    if not ctx.data_start <= as_of <= ctx.data_end:
        raise APIError('invalid-range', f'as_of ({as_of}) is outside the available data '
                                        f'({ctx.data_start}..{ctx.data_end})')
    return as_of


def _cohort_filter(cells, cohort_start, cohort_end):
    if cohort_start and cohort_end and cohort_start > cohort_end:
        raise APIError('invalid-range', f'cohort_start ({cohort_start}) is after cohort_end '
                                        f'({cohort_end})')
    if cohort_start:
        cells = cells[cells['cohort_week'] >= pd.Timestamp(bucket_start(cohort_start, 'week'))]
    if cohort_end:
        cells = cells[cells['cohort_week'] <= pd.Timestamp(bucket_start(cohort_end, 'week'))]
    return cells


def _snapped(cohort_start, cohort_end):
    notes = []
    for name, value in (('cohort_start', cohort_start), ('cohort_end', cohort_end)):
        if value and value.weekday() != 0:
            notes.append(f'{name} {value} was snapped to its cohort week '
                         f'{bucket_start(value, "week")} (Monday).')
    return notes


def _rate(point):
    return metrics.ratio(point['active_users'], point['cohort_users'])


def retention(conn, as_of, cohort_start, cohort_end):
    ctx = context.load(conn)
    as_of = _resolve_as_of(ctx, as_of)
    cells = complete_as_of(_cohort_filter(repo.cells(conn, MAX_WEEK), cohort_start, cohort_end),
                           as_of)
    curve = pooled_curve(cells, as_of, range(MAX_WEEK + 1))
    by_week = {p['week']: p for p in curve}
    sizes = cells[cells['weeks_since_signup'] == 0]

    months = []
    for month, group in sizes.groupby(sizes['cohort_week'].dt.to_period('M')):
        subset = cells[cells['cohort_week'].isin(group['cohort_week'])]
        pm = {p['week']: p for p in pooled_curve(subset, as_of, (1, 4))}
        months.append(CohortMonth(month=month.start_time.date(), cohorts=len(group),
                                  cohort_users=int(group['cohort_size'].sum()),
                                  week_1_rate=_rate(pm[1]), week_4_rate=_rate(pm[4])))
    data = RetentionData(
        as_of=as_of, cohorts=len(sizes), users_in_cohorts=int(sizes['cohort_size'].sum()),
        pooled_curve=[PooledWeek(week=p['week'], retention_rate=_rate(p),
                                 cohorts_included=p['cohorts'], users_included=p['cohort_users'],
                                 active_users=p['active_users']) for p in curve],
        headline=Headline(**{f'week_{w}': _rate(by_week[w]) for w in (1, 4, 8, 12)}),
        by_cohort_month=months)
    return context.envelope(
        RetentionData, data, ctx, repo.SOURCES, as_of=as_of,
        filters={'cohort_start': cohort_start, 'cohort_end': cohort_end},
        caveats=[CAVEAT, *_snapped(cohort_start, cohort_end)], anchor='retention')


def cohorts(conn, as_of, cohort_start, cohort_end, weeks):
    ctx = context.load(conn)
    as_of = _resolve_as_of(ctx, as_of)
    cells = _cohort_filter(repo.cells(conn, MAX_WEEK), cohort_start, cohort_end)
    complete = complete_as_of(cells, as_of)
    sizes = complete[complete['weeks_since_signup'] == 0].sort_values('cohort_week')
    if not cohort_start and not cohort_end:
        sizes = sizes.tail(DEFAULT_COHORTS)
    if len(sizes) > MAX_COHORTS:
        raise APIError('invalid-range', f'{len(sizes)} cohorts selected; at most {MAX_COHORTS} '
                                        f'per request (narrow cohort_start/cohort_end)')
    active = {(r.cohort_week, r.weeks_since_signup): int(r.active_users)
              for r in complete.itertuples()}
    rows = []
    for c in sizes.itertuples():
        size, row = int(c.cohort_size), []
        for week in range(weeks + 1):
            week_end = c.cohort_week.date() + timedelta(days=7 * (week + 1) - 1)
            if week_end > as_of:
                row.append(None)
            else:
                n = active.get((c.cohort_week, week), 0)   # ended week, no row: nobody active
                row.append(CohortCell(week=week, active_users=n,
                                      retention_rate=metrics.ratio(n, size)))
        rows.append(CohortRow(cohort_week=c.cohort_week.date(), cohort_size=size, cells=row))
    data = CohortMatrix(as_of=as_of, weeks=list(range(weeks + 1)), cohorts=rows)
    caveats = [CAVEAT, *_snapped(cohort_start, cohort_end)]
    if not cohort_start and not cohort_end:
        caveats.append(f'Showing the latest {DEFAULT_COHORTS} cohorts with a complete first week; '
                       f'pass cohort_start/cohort_end for others.')
    return context.envelope(
        CohortMatrix, data, ctx, repo.SOURCES, as_of=as_of,
        filters={'cohort_start': cohort_start, 'cohort_end': cohort_end, 'weeks': weeks},
        caveats=caveats, anchor='retention')
