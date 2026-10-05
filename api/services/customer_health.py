"""Customer health (docs/metric-definitions.md#customer-health).

Scores and tiers are the persisted output of analytics/health_scoring.py
(latest snapshot in analytics.workspace_health_scores); tier bounds and input
weights come from the same module. Nothing is re-scored per request.
"""
from analytics.health_scoring import TIER_BINS, TIER_LABELS, WEIGHTS
from api import metrics
from api.errors import APIError
from api.repositories import customer_health as repo
from api.schemas.customer_health import (HealthSummary, HistogramBin, PlanHealth, TierCount,
                                         WorkspaceHealth, WorkspacePage)
from api.services import context

CAVEAT = ('Scores are percentile ranks relative to the workspaces scored together, and tiers '
          'are calibrated on the synthetic data (PHASE_2_REPORT.md); plan_tier is the '
          "workspace's current plan.")


def tier_of(score):
    """Tier of a score with analytics.health_scoring's bins (right-closed, lowest inclusive)."""
    for label, upper in zip(TIER_LABELS, TIER_BINS[1:]):
        if score <= upper:
            return label
    return TIER_LABELS[-1]


def _snapshot(conn):
    snapshot = repo.latest_snapshot(conn)
    if snapshot is None:
        raise APIError('data-not-ready', 'no health scores have been computed; run the pipeline '
                                         'analytics step')
    return snapshot


def summary(conn, plan_tier):
    ctx = context.load(conn)
    snapshot = _snapshot(conn)
    s = repo.summary(conn, snapshot, plan_tier)
    counts = repo.tiers(conn, snapshot, plan_tier)
    bins = repo.histogram(conn, snapshot, plan_tier)
    width = repo.BIN_WIDTH
    plans = {}
    for r in repo.by_plan(conn, snapshot):
        p = plans.setdefault(r['plan_tier'], {'n': 0, 'score_sum': 0.0,
                                              'tiers': dict.fromkeys(TIER_LABELS, 0)})
        p['n'] += r['n']
        p['score_sum'] += r['score_sum']
        p['tiers'][r['risk_tier']] = r['n']
    data = HealthSummary(
        snapshot_date=snapshot, workspaces=s['workspaces'],
        mean_score=None if s['mean_score'] is None else round(s['mean_score'], 1),
        tiers=[TierCount(tier=label, min=TIER_BINS[i], max=TIER_BINS[i + 1],
                         workspaces=counts.get(label, 0),
                         share=metrics.ratio(counts.get(label, 0), s['workspaces']))
               for i, label in enumerate(TIER_LABELS)],
        histogram=[HistogramBin(bin_start=(b - 1) * width, bin_end=b * width,
                                workspaces=bins.get(b, 0), tier=tier_of(b * width))
                   for b in range(1, 100 // width + 1)],
        by_plan=[PlanHealth(plan_tier=plan, workspaces=p['n'],
                            mean_score=round(p['score_sum'] / p['n'], 1) if p['n'] else None,
                            tiers=p['tiers'])
                 for plan, p in sorted(plans.items()) if plan_tier in (None, plan)],
        weights=WEIGHTS)
    return context.envelope(HealthSummary, data, ctx, repo.SOURCES, as_of=snapshot,
                            filters={'plan_tier': plan_tier}, caveats=[CAVEAT],
                            anchor='customer-health')


def workspaces(conn, tier, plan_tier, sort, order, limit, offset):
    ctx = context.load(conn)
    snapshot = _snapshot(conn)
    total = repo.count_workspaces(conn, snapshot, tier, plan_tier)
    rows = repo.workspaces(conn, snapshot, tier, plan_tier, sort, order, limit, offset)
    items = [WorkspaceHealth(**{k: (float(v) if k in ('health_score', 'dau_over_seats_ratio',
                                                      'pct_ai_calls_automated', 'nps_score',
                                                      'mrr_usd', 'mrr_change_usd')
                                    and v is not None else v) for k, v in r.items()})
             for r in rows]
    data = WorkspacePage(snapshot_date=snapshot, total=total, limit=limit, offset=offset,
                         next_offset=offset + limit if offset + limit < total else None,
                         sort=sort, order=order, items=items)
    return context.envelope(WorkspacePage, data, ctx, repo.SOURCES, as_of=snapshot,
                            filters={'tier': tier, 'plan_tier': plan_tier, 'sort': sort,
                                     'order': order}, caveats=[CAVEAT], anchor='customer-health')
