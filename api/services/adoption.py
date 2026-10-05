"""Feature adoption curves and monthly usage (docs/metric-definitions.md#feature-adoption)."""
from analytics.features import AI_FEATURES, FEATURE_MAP
from api import metrics
from api.errors import APIError
from api.params import month_window_dates, resolve_months
from api.repositories import adoption as repo
from api.schemas.adoption import (AdoptionAt, AdoptionData, Curve, CurvePoint, FeatureMonth,
                                  UsageMonth)
from api.services import context

FEATURES = sorted(set(FEATURE_MAP.values()))
MAX_FEATURES = 10
CENSORING = ('cumulative_rate divides by all users, including users who signed up too recently '
             'to be observed at day D, so it understates adoption at later days; observed_rate '
             'only counts users with at least D days of history.')


def _features(requested):
    if not requested:
        return FEATURES
    unknown = sorted(set(requested) - set(FEATURES))
    if unknown:
        raise APIError('invalid-parameter', f"unknown feature(s): {', '.join(unknown)}; "
                                            f"valid: {', '.join(FEATURES)}")
    if len(set(requested)) > MAX_FEATURES:
        raise APIError('invalid-parameter', f'at most {MAX_FEATURES} features per request')
    return sorted(set(requested))


def _curves(rows, features, max_day):
    by_feature = {f: [] for f in features}
    for r in rows:
        by_feature[r['feature_name']].append(r)
    curves, at = [], []
    for feature in features:
        cumulative, points = 0, []
        total = by_feature[feature][0]['total_users'] if by_feature[feature] else 0
        for r in by_feature[feature]:          # days 0..90 in order
            cumulative += r['users_adopted']
            points.append(CurvePoint(day=r['days_since_signup'],
                                     cumulative_rate=metrics.ratio(cumulative, r['total_users']),
                                     observed_rate=metrics.ratio(r['eligible_adopters'],
                                                                 r['eligible_users']),
                                     eligible_users=r['eligible_users']))
        by_day = {p.day: p for p in points}
        curves.append(Curve(feature=feature, is_ai=feature in AI_FEATURES, total_users=total,
                            points=[p for p in points if p.day <= max_day]))
        at.append(AdoptionAt(feature=feature, day_7=by_day.get(7), day_30=by_day.get(30),
                             day_90=by_day.get(90)))
    return curves, at


def build(conn, features, max_day, start_month, end_month):
    features = _features(features)
    ctx = context.load(conn)
    first, last = repo.month_bounds(conn)
    if last is None:
        raise APIError('data-not-ready', 'the monthly activity table is empty; run the pipeline')
    window = resolve_months(start_month, end_month, first, last)
    curves, at = _curves(repo.curves(conn, features), features, max_day)

    usage = {}
    for r in repo.usage(conn, window.start, window.end, features):
        usage.setdefault(r['month_start'], {})[r['feature_name']] = r
    monthly = []
    for m in repo.activity(conn, window.start, window.end):
        month_usage = usage.get(m['month_start'], {})
        monthly.append(UsageMonth(
            month=m['month_start'], is_complete=m['month_complete'],
            active_workspaces=m['active_workspaces'],
            feature_active_workspaces=m['feature_active_workspaces'],
            ai_active_workspaces=m['ai_active_workspaces'],
            ai_adoption_rate=metrics.ratio(m['ai_active_workspaces'],
                                           m['feature_active_workspaces']),
            by_feature={f: FeatureMonth(
                active_workspaces=month_usage.get(f, {}).get('active_workspaces', 0),
                active_users=month_usage.get(f, {}).get('active_users', 0),
                workspace_share=metrics.ratio(month_usage.get(f, {}).get('active_workspaces', 0),
                                              m['feature_active_workspaces']))
                for f in features}))
    caveats = [CENSORING]
    if monthly and not monthly[-1].is_complete:
        caveats.append(f'{monthly[-1].month:%Y-%m} is incomplete.')
    return context.envelope(
        AdoptionData, AdoptionData(curves=curves, adoption_at=at, monthly=monthly), ctx,
        repo.SOURCES, window=month_window_dates(window, ctx.data_end),
        filters={'features': ','.join(features), 'max_day': max_day}, caveats=caveats,
        anchor='feature-adoption')
