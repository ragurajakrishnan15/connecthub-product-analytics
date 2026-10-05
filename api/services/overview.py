"""Headline KPIs (PHASE_4_PLAN.md §5.3; definitions in docs/metric-definitions.md).

Every KPI is computed from the same serving tables and functions as its
domain endpoint, over a fixed period anchored at the last loaded day, and is
compared with the period before. A period that starts before the first loaded
day is incomplete: its value is null rather than computed on truncated data.
"""
from datetime import timedelta

from analytics.health_scoring import TIER_LABELS
from analytics.retention import as_of as complete_as_of
from analytics.retention import pooled_curve
from api import metrics
from api.params import add_months, bucket_end
from api.repositories import activation as activation_repo
from api.repositories import customer_health as health_repo
from api.repositories import engagement as engagement_repo
from api.repositories import experiments as experiments_repo
from api.repositories import nps as nps_repo
from api.repositories import overview as repo
from api.repositories import retention as retention_repo
from api.repositories import support as support_repo
from api.schemas.common import Kpi, Period
from api.schemas.overview import (Kpis, OverviewData, OverviewExperiments, OverviewHealth,
                                  TierShare)
from api.services import context
from api.services.nps import nps_value
from experimentation.decision import DECISION_CODES, decision_code
from experimentation.experiments import EXPERIMENTS

NPS_MIN_RESPONSES = 30
INCOMPLETE = 'period starts before the first loaded day'


def _kpi(value, previous, period, previous_period, unit, definition, note=None):
    change_abs, change_rel = metrics.change(value, previous)
    return Kpi(value=value, previous_value=previous, change_abs=change_abs, change_rel=change_rel,
               period=Period(start=period[0], end=period[1]),
               previous_period=Period(start=previous_period[0], end=previous_period[1]),
               unit=unit, definition=definition, note=note)


def _days(end, n):
    return end - timedelta(days=n - 1), end


def _windowed(ctx, period, compute):
    """compute(start, end) unless the period starts before the data does."""
    return None if period[0] < ctx.data_start else compute(*period)


def _month_period(m):
    return (m, bucket_end(m, 'month'))


def _monthly_kpi(rows, value_of, unit, definition, empty_note):
    if not rows:
        return None
    current = rows[0]
    previous = rows[1] if len(rows) > 1 else None
    period = _month_period(current['month_start'])
    previous_period = _month_period(previous['month_start'] if previous
                                    else add_months(current['month_start'], -1))
    return _kpi(value_of(current), value_of(previous) if previous else None, period,
                previous_period, unit, definition, None if previous else empty_note)


def build(conn):
    ctx = context.load(conn)
    end = ctx.data_end

    mrr = _monthly_kpi(
        repo.complete_revenue_months(conn), lambda r: round(r['mrr_usd'], 2), 'usd',
        'MRR of the latest complete month vs the month before', 'no earlier complete month')
    ai_adoption = _monthly_kpi(
        repo.complete_activity_months(conn),
        lambda r: metrics.ratio(r['ai_active_workspaces'], r['feature_active_workspaces']),
        'fraction', 'AI-active / feature-active workspaces, latest complete month vs the month '
                    'before', 'no earlier complete month')

    def dau_on(d):
        row = None if d < ctx.data_start else engagement_repo.day(conn, d)
        return None if row is None else row['dau']
    prev_day = end - timedelta(days=28)
    dau = _kpi(dau_on(end), dau_on(prev_day), (end, end), (prev_day, prev_day), 'users',
               'Distinct active users on the last loaded day vs 28 days earlier (same weekday)')

    cells = retention_repo.cells(conn)

    def week4(d):
        if d < ctx.data_start:
            return None
        point = pooled_curve(complete_as_of(cells, d), d, (4,))[0]
        return metrics.ratio(point['active_users'], point['cohort_users'])
    retention = _kpi(week4(end), week4(prev_day), (ctx.data_start, end),
                     (ctx.data_start, prev_day), 'fraction',
                     'Pooled week-4 retention over cohorts whose week 4 has ended, as of the last '
                     'loaded day vs as of 28 days earlier')

    complete_through = end - timedelta(days=14)
    act_period = _days(complete_through, 30)
    act_previous = _days(act_period[0] - timedelta(days=1), 30)

    def activation_rate(s, e):
        t = activation_repo.totals(conn, s, e, None, False)
        return metrics.ratio(t['activated_14d'], t['signups'])
    activation = _kpi(_windowed(ctx, act_period, activation_rate),
                      _windowed(ctx, act_previous, activation_rate), act_period, act_previous,
                      'fraction', 'Activated within 14 days / signups, over the latest 30 signup '
                                  'days with a complete 14-day window vs the 30 days before')

    nps_period = _days(end, 90)
    nps_previous = _days(nps_period[0] - timedelta(days=1), 90)

    def nps_score(s, e):
        return nps_value(nps_repo.totals(conn, s, e, None), NPS_MIN_RESPONSES)['nps']
    nps = _kpi(_windowed(ctx, nps_period, nps_score), _windowed(ctx, nps_previous, nps_score),
               nps_period, nps_previous, 'nps_points',
               f'NPS over the last 90 days vs the 90 days before (null below '
               f'{NPS_MIN_RESPONSES} responses)')

    res_period = _days(end, 30)
    res_previous = _days(res_period[0] - timedelta(days=1), 30)

    def resolution(s, e):
        a = support_repo.agent(conn, s, e, None, None)
        return metrics.ratio(a['ai_resolved'], a['calls'])
    ai_resolution = _kpi(_windowed(ctx, res_period, resolution),
                         _windowed(ctx, res_previous, resolution), res_period, res_previous,
                         'fraction', 'AI-handled calls resolved without escalation / AI-handled '
                                     'calls, last 30 days vs the 30 days before')

    def no_complete_month(unit):     # data shorter than one complete month
        return _kpi(None, None, (end, end), (end, end), unit, 'no complete month loaded',
                    'no complete month loaded')
    kpis = Kpis(mrr_usd=mrr or no_complete_month('usd'), dau=dau, week4_retention_rate=retention,
                ai_feature_adoption_rate=ai_adoption or no_complete_month('fraction'),
                activation_rate_14d=activation, nps=nps, ai_resolution_rate=ai_resolution)
    for k in (kpis.activation_rate_14d, kpis.nps, kpis.ai_resolution_rate, kpis.dau,
              kpis.week4_retention_rate):
        if k.note is None and (k.period.start < ctx.data_start
                               or k.previous_period.start < ctx.data_start):
            k.note = INCOMPLETE

    snapshot = health_repo.latest_snapshot(conn)
    counts = health_repo.tiers(conn, snapshot, None) if snapshot else {}
    total = sum(counts.values())
    health = OverviewHealth(snapshot_date=snapshot, workspaces=total,
                            tiers={t: TierShare(workspaces=counts.get(t, 0),
                                                share=metrics.ratio(counts.get(t, 0), total))
                                   for t in TIER_LABELS})

    results = experiments_repo.results(conn)
    codes = [r['result'].get('decision_code') or decision_code(r['decision'])
             for e, r in results.items() if e in EXPERIMENTS]
    experiments = OverviewExperiments(registered=len(EXPERIMENTS), evaluated=len(codes),
                                      by_decision={c: codes.count(c) for c in DECISION_CODES})
    return context.envelope(OverviewData, OverviewData(kpis=kpis, health=health,
                                                       experiments=experiments),
                            ctx, repo.SOURCES, caveats=[
                                'Each KPI states its own period; values for periods that start '
                                'before the first loaded day are null.'],
                            anchor='serving-models-read-by-the-api')
