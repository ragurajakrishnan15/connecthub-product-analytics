"""DAU / WAU / MAU trend (docs/metric-definitions.md#engagement)."""
from datetime import timedelta

from api import metrics
from api.params import buckets, check_granularity, resolve_dates
from api.repositories import engagement as repo
from api.schemas.engagement import EngagementData, EngagementPoint, EngagementSummary
from api.services import context

DEFAULT_DAYS = 365
MAU_DAYS = 28


def build(conn, start, end, granularity):
    ctx = context.load(conn)
    window = resolve_dates(start, end, ctx.data_start, ctx.data_end, DEFAULT_DAYS)
    check_granularity(granularity, window)
    full_mau_from = ctx.data_start + timedelta(days=MAU_DAYS - 1)

    rows = repo.periods(conn, window.start, window.end, granularity)
    points = []
    for nominal, covered_start, covered_end, complete in buckets(window, granularity, ctx.data_end):
        r = rows.get(nominal)
        if r is None:          # every calendar day has a row; only an empty table gets here
            continue
        points.append(EngagementPoint(
            period_start=covered_start, period_end=covered_end, is_complete=complete,
            days=r['days'], avg_dau=round(r['avg_dau'], 2),
            avg_active_workspaces=round(r['avg_active_workspaces'], 2),
            wau_7d_end=r['wau_7d_end'], mau_28d_end=r['mau_28d_end'],
            avg_stickiness_rate=None if r['avg_stickiness_rate'] is None
            else round(r['avg_stickiness_rate'], 6),
            mau_window_complete=r['last_day'] >= full_mau_from))

    last = repo.day(conn, window.end)
    summary = EngagementSummary(
        day=window.end, dau=last['dau'], wau_7d=last['wau_7d'], mau_28d=last['mau_28d'],
        active_workspaces=last['active_workspaces'],
        stickiness_rate=metrics.ratio(last['dau'], last['mau_28d']),
        mau_window_complete=window.end >= full_mau_from)
    caveats = []
    if window.start < full_mau_from:
        caveats.append(f'MAU (28-day) and WAU windows before {full_mau_from} start before the '
                       f'first loaded day ({ctx.data_start}) and are understated.')
    return context.envelope(
        EngagementData, EngagementData(summary=summary, granularity=granularity, points=points),
        ctx, repo.SOURCES, window=window, filters={'granularity': granularity},
        caveats=caveats, anchor='engagement')
