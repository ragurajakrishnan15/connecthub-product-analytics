"""Integration: every API-serving gold model equals an independent pandas
computation from the generated parquet files (no SQL shared with dbt).

Needs the local pipeline to have run on the data in data/ (same skip rules as
test_warehouse_parity.py). Counts must match exactly; money and rates to 1e-6.
"""
import os

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, text

import ingest_events as ingest
from analytics.features import AI_FEATURES, FEATURE_MAP

DATA = 'data'
AI_EVENTS = ('ai_assist.used', 'ai_voice_agent.activated')
pytestmark = pytest.mark.integration


@pytest.fixture(scope='module')
def warehouse():
    if not os.environ.get('POSTGRES_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD not set; no PostgreSQL configured')
    if not os.path.exists(os.path.join(DATA, 'events.parquet')):
        pytest.skip('data/ has not been generated')
    engine = create_engine(ingest.database_url())
    try:
        with engine.connect() as conn:
            loaded = conn.execute(text('SELECT COUNT(*) FROM bronze.events_raw')).scalar()
            conn.execute(text('SELECT 1 FROM gold.fct_activation_daily LIMIT 1'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built: {exc}')
    import pyarrow.parquet as pq
    if pq.ParquetFile(os.path.join(DATA, 'events.parquet')).metadata.num_rows != loaded:
        pytest.skip('data/ differs from what is loaded; rerun ingest and dbt')
    yield engine
    engine.dispose()


@pytest.fixture(scope='module')
def src():
    """Parquet inputs, typed like the warehouse, plus the billed-plan lookup."""
    def read(name, **kw):
        return pd.read_parquet(os.path.join(DATA, f'{name}.parquet'), **kw)

    events = read('events', columns=['event_id', 'user_id', 'workspace_id', 'event_name',
                                     'event_date']).drop_duplicates('event_id')
    events['event_date'] = pd.to_datetime(events['event_date'])
    users = read('users', columns=['user_id', 'workspace_id', 'signup_date'])
    users['signup_date'] = pd.to_datetime(users['signup_date'])
    workspaces = read('workspaces', columns=['workspace_id', 'plan_tier'])
    subs = read('subscriptions')
    subs['month_start'] = pd.to_datetime(subs['month_start'])
    plans = subs.set_index(['workspace_id', 'month_start'])['plan_tier']
    current = workspaces.set_index('workspace_id')['plan_tier']

    def billed_plan(workspace_ids, dates):
        months = pd.to_datetime(dates).dt.to_period('M').dt.start_time
        idx = pd.MultiIndex.from_arrays([workspace_ids, months])
        billed = pd.Series(plans.reindex(idx).to_numpy(), index=workspace_ids.index)
        return billed.fillna(pd.Series(current.reindex(workspace_ids).to_numpy(),
                                       index=workspace_ids.index))

    return {'events': events, 'users': users, 'subs': subs, 'billed_plan': billed_plan,
            'data_end': events['event_date'].max(), 'read': read}


def gold(engine, name, keys):
    df = pd.read_sql(f'SELECT * FROM gold.{name}', engine)
    for k in keys:
        if 'date' in k or k == 'month_start':
            df[k] = pd.to_datetime(df[k])
    return df.sort_values(keys).reset_index(drop=True)


def assert_same(actual, expected, keys):
    expected = expected.sort_values(keys).reset_index(drop=True)
    assert list(actual[keys].astype(str).itertuples(index=False)) == \
        list(expected[keys].astype(str).itertuples(index=False)), 'grain differs'
    for col in expected.columns:
        if col in keys:
            continue
        a, b = actual[col], expected[col]
        if a.dtype == bool or b.dtype == bool:
            assert (a.astype(bool).to_numpy() == b.astype(bool).to_numpy()).all(), col
        else:
            assert np.allclose(a.astype(float), b.astype(float), atol=1e-6, rtol=0), col


def per_user_activation(src):
    ev = src['events'].merge(src['users'][['user_id', 'signup_date']], on='user_id')
    within = ev[ev['event_date'] <= ev['signup_date'] + pd.Timedelta(days=14)]
    u = src['users'].set_index('user_id')

    def reached(names):
        return u.index.isin(within.loc[within['event_name'].isin(names), 'user_id'])

    def first(names):
        return ev[ev['event_name'].isin(names)].groupby('user_id')['event_date'].min() \
            .reindex(u.index)

    out = pd.DataFrame({
        'signup_date': u['signup_date'],
        'plan_tier': src['billed_plan'](u['workspace_id'], u['signup_date']),
        'call': reached(['call.started']).astype(int),
        'ai': reached(AI_EVENTS).astype(int),
        'invite': reached(['team.member_invited']).astype(int),
        'first_call': first(['call.started']), 'first_ai': first(AI_EVENTS),
        'first_invite': first(['team.member_invited']),
    })
    out['activated'] = out['call'] * out['ai'] * out['invite']
    return out


def test_activation_daily(warehouse, src):
    u = per_user_activation(src)
    exp = u.groupby(['signup_date', 'plan_tier']).agg(
        signups=('call', 'size'), placed_first_call=('call', 'sum'),
        used_ai_feature=('ai', 'sum'), invited_team_member=('invite', 'sum'),
        call_and_ai=('call', lambda s: (s * u.loc[s.index, 'ai']).sum()),
        activated_14d=('activated', 'sum')).reset_index()
    exp['window_14d_complete'] = exp['signup_date'] + pd.Timedelta(days=14) <= src['data_end']
    keys = ['signup_date', 'plan_tier']
    assert_same(gold(warehouse, 'fct_activation_daily', keys), exp, keys)


def test_activation_milestone_days(warehouse, src):
    u = per_user_activation(src)
    parts = []
    for milestone, flag, col in (('placed_first_call', 'call', 'first_call'),
                                 ('used_ai_feature', 'ai', 'first_ai'),
                                 ('invited_team_member', 'invite', 'first_invite')):
        r = u[u[flag] == 1]
        parts.append(pd.DataFrame({'signup_date': r['signup_date'], 'plan_tier': r['plan_tier'],
                                   'milestone': milestone,
                                   'days_to_milestone': (r[col] - r['signup_date']).dt.days}))
    a = u[u['activated'] == 1]
    last = a[['first_call', 'first_ai', 'first_invite']].max(axis=1)
    parts.append(pd.DataFrame({'signup_date': a['signup_date'], 'plan_tier': a['plan_tier'],
                               'milestone': 'activated_14d',
                               'days_to_milestone': (last - a['signup_date']).dt.days}))
    keys = ['signup_date', 'plan_tier', 'milestone', 'days_to_milestone']
    exp = pd.concat(parts).groupby(keys).size().rename('users').reset_index()
    exp['window_14d_complete'] = exp['signup_date'] + pd.Timedelta(days=14) <= src['data_end']
    assert exp['days_to_milestone'].between(0, 14).all()
    assert_same(gold(warehouse, 'fct_activation_milestone_days', keys), exp, keys)


def test_revenue_monthly(warehouse, src):
    s = src['subs'].sort_values(['workspace_id', 'month_start']).copy()
    s['mrr_usd'] = s['mrr_usd'].astype(float)
    prev = s.groupby('workspace_id')['mrr_usd'].shift()
    mrr = s['mrr_usd']
    s['movement'] = np.select(
        [prev.isna() & (mrr > 0), (prev == 0) & (mrr > 0), (prev > 0) & (mrr == 0),
         mrr > prev, mrr < prev, mrr > 0],
        ['new', 'reactivation', 'churned', 'expansion', 'contraction', 'retained'], 'inactive')
    s['previous_mrr_usd'] = prev.fillna(0)
    s['change'] = mrr - s['previous_mrr_usd']
    g = s.groupby(['month_start', 'plan_tier'])
    exp = g.agg(workspaces=('mrr_usd', 'size'),
                paying_workspaces=('mrr_usd', lambda x: (x > 0).sum()),
                billed_seats=('billed_seats', 'sum'), mrr_usd=('mrr_usd', 'sum'),
                previous_mrr_usd=('previous_mrr_usd', 'sum')).reset_index()
    for m in ('new', 'expansion', 'reactivation', 'contraction', 'churned'):
        part = s[s['movement'] == m].groupby(['month_start', 'plan_tier'])
        exp = exp.merge(part['change'].sum().rename(f'{m}_mrr_usd').reset_index(), how='left',
                        on=['month_start', 'plan_tier'])
        exp = exp.merge(part.size().rename(f'{m}_workspaces').reset_index(), how='left',
                        on=['month_start', 'plan_tier'])
    exp = exp.fillna(0)
    exp['month_complete'] = (exp['month_start'] + pd.offsets.MonthEnd(0)) <= src['data_end']
    keys = ['month_start', 'plan_tier']
    assert_same(gold(warehouse, 'fct_revenue_monthly', keys), exp, keys)


def test_nps_daily(warehouse, src):
    n = src['read']('nps_responses')
    n['response_date'] = pd.to_datetime(n['response_date'])
    n['plan_tier'] = src['billed_plan'](n['workspace_id'], n['response_date'])
    exp = n.groupby(['response_date', 'plan_tier'])['score'].agg(
        responses='size', promoters=lambda x: (x >= 9).sum(),
        passives=lambda x: x.between(7, 8).sum(), detractors=lambda x: (x <= 6).sum(),
        score_sum='sum').reset_index()
    keys = ['response_date', 'plan_tier']
    assert_same(gold(warehouse, 'fct_nps_daily', keys), exp, keys)


def test_support_daily(warehouse, src):
    e = src['events'].copy()
    e['plan_tier'] = src['billed_plan'](e['workspace_id'], e['event_date'])
    exp = e.groupby(['event_date', 'plan_tier']).agg(
        active_users=('user_id', 'nunique'),
        tickets_created=('event_name', lambda x: (x == 'support.ticket_created').sum()),
        tickets_resolved=('event_name', lambda x: (x == 'support.ticket_resolved').sum()),
    ).reset_index()
    keys = ['event_date', 'plan_tier']
    assert_same(gold(warehouse, 'fct_support_daily', keys), exp, keys)


def test_agent_performance_daily(warehouse, src):
    a = src['read']('agent_evaluations')
    a['call_date'] = pd.to_datetime(a['call_date'])
    a['plan_tier'] = src['billed_plan'](a['workspace_id'], a['call_date'])
    a['call_type'] = a['call_type'].fillna('unknown')
    path = np.where(a['resolved_by_ai'] & ~a['escalated_to_human'], 'ai_resolved',
                    np.where(a['escalated_to_human'], 'escalated', 'human_handled'))
    a['csat_score'] = a['csat_score'].astype(float)
    keys = ['call_date', 'plan_tier', 'call_type']
    exp = a.assign(ai=path == 'ai_resolved', esc=path == 'escalated',
                   hum=path == 'human_handled').groupby(keys).agg(
        calls=('ai', 'size'), ai_resolved=('ai', 'sum'), escalated=('esc', 'sum'),
        human_handled=('hum', 'sum'), csat_sum=('csat_score', 'sum'),
        csat_count=('csat_score', 'count'),
        handle_time_seconds_sum=('handle_time_seconds', 'sum'),
        handle_time_count=('handle_time_seconds', 'count')).reset_index()
    assert_same(gold(warehouse, 'fct_agent_performance_daily', keys), exp, keys)


def test_feature_usage_and_activity_monthly(warehouse, src):
    e = src['events'].copy()
    e['month_start'] = e['event_date'].dt.to_period('M').dt.start_time
    e['feature_name'] = e['event_name'].map(FEATURE_MAP)
    f = e.dropna(subset=['feature_name'])
    complete = lambda m: (m + pd.offsets.MonthEnd(0)) <= src['data_end']  # noqa: E731

    usage = f.groupby(['month_start', 'feature_name']).agg(
        active_workspaces=('workspace_id', 'nunique'), active_users=('user_id', 'nunique'),
        usage_events=('event_id', 'size')).reset_index()
    usage['month_complete'] = complete(usage['month_start'])
    keys = ['month_start', 'feature_name']
    assert_same(gold(warehouse, 'fct_feature_usage_monthly', keys), usage, keys)

    ai = f[f['feature_name'].isin(AI_FEATURES)]
    activity = pd.DataFrame({
        'active_users': e.groupby('month_start')['user_id'].nunique(),
        'active_workspaces': e.groupby('month_start')['workspace_id'].nunique(),
        'feature_active_users': f.groupby('month_start')['user_id'].nunique(),
        'feature_active_workspaces': f.groupby('month_start')['workspace_id'].nunique(),
        'ai_active_users': ai.groupby('month_start')['user_id'].nunique(),
        'ai_active_workspaces': ai.groupby('month_start')['workspace_id'].nunique(),
    }).fillna(0).reset_index()
    activity['month_complete'] = complete(activity['month_start'])
    assert_same(gold(warehouse, 'fct_activity_monthly', ['month_start']), activity,
                ['month_start'])


def test_experiment_activation_curve(warehouse, src):
    a = src['read']('experiment_assignments', columns=['experiment_id', 'user_id', 'variant'])
    u = per_user_activation(src)
    p = a.merge(u, left_on='user_id', right_index=True)
    p = p[p['signup_date'] + pd.Timedelta(days=14) <= src['data_end']]
    last = p[['first_call', 'first_ai', 'first_invite']].max(axis=1)
    p['day'] = np.where(p['activated'] == 1, (last - p['signup_date']).dt.days, np.nan)
    rows = []
    for (exp_id, variant), g in p.groupby(['experiment_id', 'variant']):
        for day in range(15):
            n = int((g['day'] <= day).sum())
            rows.append((exp_id, variant, day, len(g), n, round(n / len(g), 6)))
    exp = pd.DataFrame(rows, columns=['experiment_id', 'variant', 'day_since_signup',
                                      'users_in_window', 'activated_cumulative',
                                      'cumulative_activation_rate'])
    keys = ['experiment_id', 'variant', 'day_since_signup']
    assert_same(gold(warehouse, 'fct_experiment_activation_curve', keys), exp, keys)


def test_curve_day14_equals_persisted_evaluation(warehouse):
    """The curve's end point is the primary metric the evaluation persisted."""
    curve = pd.read_sql('SELECT experiment_id, variant, cumulative_activation_rate '
                        'FROM gold.fct_experiment_activation_curve WHERE day_since_signup = 14',
                        warehouse)
    results = pd.read_sql('SELECT experiment_id, control_rate, treatment_rate '
                          'FROM analytics.experiment_results', warehouse).set_index('experiment_id')
    assert not curve.empty
    for r in curve.itertuples():
        expected = results.loc[r.experiment_id,
                               'control_rate' if r.variant == 'variant_0' else 'treatment_rate']
        assert round(float(r.cumulative_activation_rate), 4) == pytest.approx(expected, abs=1e-9)


def test_feature_adoption_observed_columns(warehouse, src):
    e = src['events']
    users = src['users']
    f = e.assign(feature_name=e['event_name'].map(FEATURE_MAP)).dropna(subset=['feature_name'])
    f = f.merge(users[['user_id', 'signup_date']], on='user_id')
    f = f[f['event_date'] >= f['signup_date']]
    first = f.groupby(['feature_name', 'user_id', 'signup_date'])['event_date'].min().reset_index()
    first['day'] = (first['event_date'] - first['signup_date']).dt.days
    rows = []
    for feature, g in first.groupby('feature_name'):
        for d in range(91):
            observable = users['signup_date'] + pd.Timedelta(days=d) <= src['data_end']
            adopters = g[(g['day'] <= d)
                         & (g['signup_date'] + pd.Timedelta(days=d) <= src['data_end'])]
            n = int(observable.sum())
            rows.append((feature, d, n, len(adopters),
                         round(100 * len(adopters) / n, 2) if n else np.nan))
    exp = pd.DataFrame(rows, columns=['feature_name', 'days_since_signup', 'eligible_users',
                                      'eligible_adopters', 'observed_adoption_pct'])
    keys = ['feature_name', 'days_since_signup']
    got = gold(warehouse, 'fct_feature_adoption', keys)
    assert_same(got[keys + ['eligible_users', 'eligible_adopters', 'observed_adoption_pct']],
                exp, keys)
