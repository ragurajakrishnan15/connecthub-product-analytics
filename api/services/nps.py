"""Net Promoter Score (docs/metric-definitions.md#nps)."""
from api import metrics
from api.params import buckets, check_granularity, resolve_dates
from api.repositories import nps as repo
from api.schemas.nps import NpsData, NpsPoint, NpsValue, PlanNps
from api.services import context

DEFAULT_DAYS = 90
EMPTY = {'responses': 0, 'promoters': 0, 'passives': 0, 'detractors': 0}


def nps_value(counts, min_responses):
    """NPS fields from counts; the score is suppressed below min_responses."""
    n, p, d = counts['responses'], counts['promoters'], counts['detractors']
    reason = 'no_responses' if n == 0 else 'insufficient_responses' if n < min_responses else None
    return {
        **{k: counts[k] for k in EMPTY},
        'nps': None if reason else metrics.nps(p, d, n),
        'margin_of_error_95': None if reason else metrics.nps_margin_of_error(p, d, n),
        'promoter_share': metrics.ratio(p, n), 'passive_share': metrics.ratio(counts['passives'], n),
        'detractor_share': metrics.ratio(d, n), 'suppressed_reason': reason,
    }


def build(conn, start, end, plan_tier, granularity, min_responses):
    ctx = context.load(conn)
    window = resolve_dates(start, end, ctx.data_start, ctx.data_end, DEFAULT_DAYS)
    check_granularity(granularity, window)
    rows = repo.periods(conn, window.start, window.end, granularity, plan_tier)
    series = [NpsPoint(period_start=cs, period_end=ce, is_complete=complete,
                       **nps_value(rows.get(nominal, EMPTY), min_responses))
              for nominal, cs, ce, complete in buckets(window, granularity, ctx.data_end)]
    data = NpsData(
        min_responses=min_responses,
        summary=NpsValue(**nps_value(repo.totals(conn, window.start, window.end, plan_tier),
                                     min_responses)),
        by_plan=[PlanNps(plan_tier=r['plan_tier'], **nps_value(r, min_responses))
                 for r in repo.by_plan(conn, window.start, window.end, plan_tier)],
        granularity=granularity, series=series)
    return context.envelope(
        NpsData, data, ctx, repo.SOURCES, window=window,
        filters={'plan_tier': plan_tier, 'granularity': granularity,
                 'min_responses': min_responses},
        caveats=[f'NPS is suppressed (null) for groups with fewer than {min_responses} responses.'],
        anchor='nps')
