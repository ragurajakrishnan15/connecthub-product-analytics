"""14-day activation funnel by signup date (docs/metric-definitions.md#activation).

Signups whose 14-day window extends past the last loaded day may still
activate, so by default they are excluded (and counted in
excluded_incomplete_signups) rather than treated as not activated.
"""
from datetime import timedelta

from api import metrics
from api.params import Window, buckets, check_granularity, resolve_dates
from api.repositories import activation as repo
from api.schemas.activation import (ActivationData, ActivationPoint, FunnelStage, MilestoneRates,
                                    MilestoneTiming)
from api.services import context

DEFAULT_DAYS = 90
WINDOW_DAYS = 14
MILESTONES = ('placed_first_call', 'used_ai_feature', 'invited_team_member', 'activated_14d')


def build(conn, start, end, plan_tier, granularity, include_incomplete):
    ctx = context.load(conn)
    complete_through = ctx.data_end - timedelta(days=WINDOW_DAYS)
    requested = resolve_dates(start, end, ctx.data_start, ctx.data_end, DEFAULT_DAYS,
                              default_end=max(complete_through, ctx.data_start))
    check_granularity(granularity, requested)
    caveats = []

    excluded = 0
    window = requested
    if not include_incomplete:
        excluded = repo.incomplete_signups(conn, requested.start, requested.end, plan_tier)
        if requested.end > complete_through:
            window = Window(requested.start, complete_through, requested.clamped) \
                if requested.start <= complete_through else None
            caveats.append(f'Signups after {complete_through} have an incomplete 14-day window '
                           f'and are excluded ({excluded}); pass include_incomplete=true to '
                           f'include them (their activation is still accruing).')
    elif requested.end > complete_through:
        caveats.append(f'Includes signups after {complete_through} whose 14-day window is '
                       f'incomplete: their activation is understated.')

    if window is None:                       # nothing in the range has a complete window
        totals = dict.fromkeys(('signups', 'placed_first_call', 'used_ai_feature',
                                'invited_team_member', 'call_and_ai', 'activated_14d'), 0)
        timing, rows, periods = {}, {}, []
    else:
        args = (window.start, window.end, plan_tier, include_incomplete)
        totals = repo.totals(conn, *args)
        timing = repo.milestone_timing(conn, *args)
        rows = repo.periods(conn, window.start, window.end, granularity, plan_tier,
                            include_incomplete)
        periods = buckets(window, granularity, ctx.data_end)

    signups = totals['signups']
    funnel, previous = [], None
    for stage, key in (('signed_up', 'signups'), ('placed_first_call', 'placed_first_call'),
                       ('call_and_ai', 'call_and_ai'), ('activated_14d', 'activated_14d')):
        users = totals[key]
        funnel.append(FunnelStage(stage=stage, users=users,
                                  rate_of_signups=metrics.ratio(users, signups),
                                  rate_of_previous=None if previous is None
                                  else metrics.ratio(users, previous)))
        previous = users

    series = []
    for nominal, covered_start, covered_end, complete in periods:
        r = rows.get(nominal, {'signups': 0, 'activated_14d': 0})
        series.append(ActivationPoint(
            period_start=covered_start, period_end=covered_end, is_complete=complete,
            signups=r['signups'], activated_14d=r['activated_14d'],
            activation_rate_14d=metrics.ratio(r['activated_14d'], r['signups'])))

    data = ActivationData(
        signups=signups, excluded_incomplete_signups=excluded,
        complete_windows_through=complete_through,
        activation_rate_14d=metrics.ratio(totals['activated_14d'], signups),
        funnel=funnel,
        milestones=MilestoneRates(
            placed_first_call_rate=metrics.ratio(totals['placed_first_call'], signups),
            used_ai_feature_rate=metrics.ratio(totals['used_ai_feature'], signups),
            invited_team_member_rate=metrics.ratio(totals['invited_team_member'], signups)),
        time_to_milestone=[MilestoneTiming(
            milestone=m, users_reached=timing.get(m, {}).get('users_reached', 0) or 0,
            median_days=timing.get(m, {}).get('median_days'),
            p75_days=timing.get(m, {}).get('p75_days')) for m in MILESTONES],
        granularity=granularity, series=series)
    return context.envelope(
        ActivationData, data, ctx, repo.SOURCES, window=window,
        filters={'plan_tier': plan_tier, 'granularity': granularity,
                 'include_incomplete': str(include_incomplete).lower()},
        caveats=caveats, anchor='activation')
