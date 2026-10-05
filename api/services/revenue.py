"""Monthly recurring revenue (docs/metric-definitions.md#serving-models-read-by-the-api)."""
from itertools import groupby

from api import metrics
from api.errors import APIError
from api.params import month_window_dates, resolve_months
from api.repositories import revenue as repo
from api.schemas.revenue import Movement, PlanRevenue, RevenueData, RevenueMonth
from api.services import context

COMPONENTS = ('new', 'expansion', 'reactivation', 'contraction', 'churned')
TIER_CAVEAT = ('A workspace that changes plan moves all its MRR to the new plan, so per-plan '
               'previous MRR and movements are relative to the workspaces on that plan this month.')


def _sum(rows, key):
    return sum(r[key] for r in rows)


def _month(month, rows, group_by):
    mrr, previous = round(_sum(rows, 'mrr_usd'), 2), round(_sum(rows, 'previous_mrr_usd'), 2)
    paying = _sum(rows, 'paying_workspaces')
    parts = {c: round(_sum(rows, f'{c}_mrr_usd'), 2) for c in COMPONENTS}
    by_plan = None
    if group_by == 'plan_tier':
        by_plan = {r['plan_tier']: PlanRevenue(
            mrr_usd=round(r['mrr_usd'], 2), paying_workspaces=r['paying_workspaces'],
            share_of_mrr=metrics.ratio(r['mrr_usd'], mrr),
            arpa_usd=None if not r['paying_workspaces']
            else round(r['mrr_usd'] / r['paying_workspaces'], 2)) for r in rows}
    return RevenueMonth(
        month=month, is_complete=all(r['month_complete'] for r in rows),
        mrr_usd=mrr, previous_mrr_usd=previous,
        mrr_growth_rate=metrics.ratio(mrr - previous, previous),
        paying_workspaces=paying, arpa_usd=None if not paying else round(mrr / paying, 2),
        billed_seats=_sum(rows, 'billed_seats'),
        movement=Movement(**{f'{c}_usd': parts[c] for c in COMPONENTS},
                          net_new_usd=round(sum(parts.values()), 2)),
        by_plan=by_plan)


def build(conn, start_month, end_month, plan_tier, group_by):
    if group_by == 'plan_tier' and plan_tier:
        raise APIError('invalid-parameter', 'group_by=plan_tier cannot be combined with a '
                                            'plan_tier filter; use group_by=none')
    ctx = context.load(conn)
    first, last = repo.month_bounds(conn)
    if last is None:
        raise APIError('data-not-ready', 'the revenue table is empty; run the pipeline')
    window = resolve_months(start_month, end_month, first, last)
    rows = repo.months(conn, window.start, window.end, plan_tier)
    series = [_month(month, list(group), group_by)
              for month, group in groupby(rows, key=lambda r: r['month_start'])]
    complete = [m for m in series if m.is_complete]
    caveats = []
    if plan_tier or group_by == 'plan_tier':
        caveats.append(TIER_CAVEAT)
    if series and not series[-1].is_complete:
        caveats.append(f'{series[-1].month:%Y-%m} is incomplete: billing for the month is '
                       f'still accruing.')
    data = RevenueData(latest=series[-1] if series else None,
                       latest_complete=complete[-1] if complete else None, series=series)
    return context.envelope(
        RevenueData, data, ctx, repo.SOURCES, window=month_window_dates(window, ctx.data_end),
        filters={'plan_tier': plan_tier, 'group_by': group_by}, caveats=caveats,
        anchor='serving-models-read-by-the-api')
