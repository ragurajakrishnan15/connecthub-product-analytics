"""Integration: the API-serving gold models on a tiny hand-built warehouse.

A throwaway database (<POSTGRES_DB>_serving_fixture) gets a few users, events,
subscriptions, NPS responses and agent evaluations chosen to hit the edge
cases; dbt builds every model on it and each expected value below is worked
out by hand from the rows in `seed()`. Covered:

- 14-day window boundaries (day 14 counts, day 15 does not; signup on
  data_end - 14 is a complete window, data_end - 13 is not)
- non-nested milestones (invite without AI) and the strict funnel
- billed plan of the activity month vs the workspace's current plan
- MRR movements, signs and cross-month continuity
- NPS category boundaries, escalation precedence, a missing call type
- retention cells with zero active users, completeness as of a date
- right-censored feature adoption and a zero denominator (NULL)
- changes in bronze propagate (incrementally) and equal a full refresh
- the serving dbt tests fail on corrupted gold rows

Tests in this module run in order and share the database. Needs PostgreSQL.
"""
import json
import os
import subprocess
import sys
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

import ingest_events
from analytics import cohort_engine
from experimentation.assignment import ensure_table
from pipeline import config
from pipeline.fingerprint import fingerprint

pytestmark = pytest.mark.integration

DATA_END = date(2025, 3, 10)
SERVING = ['fct_activation_daily', 'fct_activation_milestone_days', 'fct_revenue_monthly',
           'fct_nps_daily', 'fct_support_daily', 'fct_agent_performance_daily',
           'fct_feature_usage_monthly', 'fct_activity_monthly',
           'fct_experiment_activation_curve', 'fct_feature_adoption']

# user -> (workspace, signup, first product activity, current plan)
USERS = {
    'u1': ('w1', '2025-01-06', '2025-01-06', 'Professional'),
    'u2': ('w1', '2025-01-06', '2025-01-07', 'Professional'),
    'u3': ('w2', '2025-02-24', '2025-02-24', 'Free'),   # signup = data_end - 14
    'u4': ('w2', '2025-02-25', '2025-02-26', 'Free'),   # signup = data_end - 13
    'u5': ('w1', '2025-03-10', None, 'Professional'),   # signup on data_end, no product use
}
EVENTS = [  # (user, date, event_name)
    ('u1', '2025-01-06', 'user.signup'), ('u1', '2025-01-06', 'call.started'),
    ('u1', '2025-01-09', 'ai_assist.used'), ('u1', '2025-01-14', 'sms.sent'),
    ('u1', '2025-01-14', 'support.ticket_created'), ('u1', '2025-01-14', 'support.ticket_resolved'),
    ('u1', '2025-01-20', 'team.member_invited'),      # day 14: inside the window
    ('u1', '2025-01-28', 'sms.sent'),
    ('u2', '2025-01-06', 'user.signup'), ('u2', '2025-01-07', 'call.started'),
    ('u2', '2025-01-08', 'team.member_invited'),
    ('u2', '2025-01-21', 'ai_assist.used'),           # day 15: outside the window
    ('u3', '2025-02-24', 'user.signup'), ('u3', '2025-02-24', 'call.started'),
    ('u3', '2025-03-10', 'support.ticket_created'),
    ('u4', '2025-02-25', 'user.signup'), ('u4', '2025-02-26', 'call.started'),
    ('u4', '2025-02-27', 'ai_assist.used'), ('u4', '2025-03-01', 'team.member_invited'),
    ('u4', '2025-03-04', 'sms.sent'),
    ('u5', '2025-03-10', 'user.signup'),
]
SUBSCRIPTIONS = [  # (workspace, month, plan, seats, price, mrr)
    ('w1', '2025-01-01', 'Essentials', 2, 15, 30),    # new
    ('w1', '2025-02-01', 'Professional', 2, 25, 50),  # expansion (+ tier change)
    ('w1', '2025-03-01', 'Professional', 1, 25, 25),  # contraction
    ('w2', '2025-01-01', 'Free', 0, 0, 0),            # inactive
    ('w2', '2025-02-01', 'Essentials', 1, 15, 15),    # reactivation
    ('w2', '2025-03-01', 'Essentials', 0, 15, 0),     # churned
]
NPS = [('n1', 'u1', 'w1', '2025-01-31', 9), ('n2', 'u2', 'w1', '2025-01-31', 8),
       ('n3', 'u3', 'w2', '2025-03-01', 6), ('n4', 'u4', 'w2', '2025-03-01', 7),
       ('n5', 'u1', 'w1', '2025-03-10', 10)]
EVALS = [  # (id, workspace, date, call_type, resolved_by_ai, escalated, csat, handle_s)
    ('e1', 'w1', '2025-01-15', 'inbound', True, False, 4.5, 100),
    ('e2', 'w1', '2025-01-15', 'inbound', True, True, 2.0, 300),    # both flags -> escalated
    ('e3', 'w1', '2025-01-15', 'inbound', False, False, None, None),
    ('e4', 'w2', '2025-03-02', None, True, False, 5.0, 60),          # missing call type
]
ASSIGNMENTS = [('u1', 'variant_1'), ('u2', 'variant_0'), ('u3', 'variant_0'), ('u4', 'variant_1')]


def insert_events(conn, events, start=0):
    rows = [{'id': f'evt-{start + i}', 'u': u, 'w': USERS[u][0], 'n': name,
             'ts': f'{d} 10:{i % 60:02d}:00', 'd': d, 's': f'{u}-{d}'}
            for i, (u, d, name) in enumerate(events)]
    conn.execute(text(
        'INSERT INTO bronze.events_raw (event_id, user_id, workspace_id, event_name, '
        "timestamp_utc, event_date, session_id, platform, country_code) "
        "VALUES (:id, :u, :w, :n, :ts, :d, :s, 'web', 'US')"), rows)


def seed(engine):
    ingest_events.create_tables(engine)
    ensure_table(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO bronze.workspaces_raw VALUES "
            "('w1', 'Fixture One', '2025-01-01', 'Professional', 5, 'US'), "
            "('w2', 'Fixture Two', '2025-01-01', 'Free', 3, 'US')"))
        conn.execute(text(
            'INSERT INTO bronze.users_raw VALUES (:u, :w, :s, :f, :p, \'US\', FALSE)'),
            [{'u': u, 'w': w, 's': s, 'f': f, 'p': p} for u, (w, s, f, p) in USERS.items()])
        insert_events(conn, EVENTS)
        conn.execute(text('INSERT INTO bronze.subscriptions VALUES (:w, :m, :p, :n, :price, :mrr)'),
                     [dict(zip(('w', 'm', 'p', 'n', 'price', 'mrr'), r)) for r in SUBSCRIPTIONS])
        conn.execute(text('INSERT INTO bronze.nps_responses VALUES (:i, :u, :w, :d, :s)'),
                     [dict(zip('iuwds', r)) for r in NPS])
        conn.execute(text('INSERT INTO bronze.agent_evaluations VALUES '
                          '(:i, :w, :d, :t, :r, :h, :c, :e)'),
                     [{'i': i, 'w': w, 'd': d, 't': t, 'r': r, 'e': e, 'c': c, 'h': h}
                      for i, w, d, t, r, e, c, h in EVALS])
        conn.execute(text(
            "INSERT INTO experiments.experiment_assignments "
            "SELECT 'exp_onboarding_v2', user_id, :v, signup_date FROM bronze.users_raw "
            "WHERE user_id = :u"), [{'u': u, 'v': v} for u, v in ASSIGNMENTS])


def dbt(db, tmp, *args):
    exe = os.path.join(os.path.dirname(sys.executable), 'dbt.exe' if os.name == 'nt' else 'dbt')
    env = dict(os.environ, POSTGRES_DB=db, DBT_TARGET_PATH=str(tmp / 'target'),
               DBT_LOG_PATH=str(tmp / 'logs'),
               POSTGRES_HOST=os.environ.get('POSTGRES_HOST', '127.0.0.1'))
    res = subprocess.run([exe, *args, '--project-dir', config.dbt_dir(),
                          '--profiles-dir', config.dbt_dir()],
                         env=env, capture_output=True, text=True, timeout=900)
    with open(tmp / 'target' / 'run_results.json') as f:
        results = {r['unique_id'].split('.')[2]: r['status'] for r in json.load(f)['results']}
    return res, results


@pytest.fixture(scope='module')
def fx(tmp_path_factory):
    if not os.environ.get('POSTGRES_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD not set; no PostgreSQL configured')
    admin = config.create_engine().execution_options(isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as conn:
            conn.execute(text('SELECT 1'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'PostgreSQL not reachable: {exc}')
    db = f"{os.environ.get('POSTGRES_DB', 'connecthub_analytics')}_serving_fixture"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    engine = config.create_engine(db)
    tmp = tmp_path_factory.mktemp('serving_dbt')
    seed(engine)
    res, _ = dbt(db, tmp, 'run')
    assert res.returncode == 0, res.stdout[-3000:]
    yield db, engine, tmp
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
    admin.dispose()


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def table(engine, name, order):
    return rows(engine, f'SELECT * FROM gold.{name} ORDER BY {order}')


def as_py(rows_):
    """Decimals -> float, dates -> ISO strings, for comparison with literals."""
    out = []
    for r in rows_:
        out.append({k: (float(v) if hasattr(v, 'as_tuple') else
                        v.isoformat() if isinstance(v, date) else v) for k, v in r.items()})
    return out


def test_serving_dbt_tests_pass_on_fixture(fx):
    db, _, tmp = fx
    res, results = dbt(db, tmp, 'test', '--select', 'tag:serving')
    assert res.returncode == 0, res.stdout[-3000:]
    assert results and all(s == 'pass' for s in results.values()), results


def test_activation_daily(fx):
    _, engine, _ = fx
    got = as_py(table(engine, 'fct_activation_daily', 'signup_date, plan_tier'))
    cols = ('signup_date', 'plan_tier', 'signups', 'placed_first_call', 'used_ai_feature',
            'invited_team_member', 'call_and_ai', 'activated_14d', 'window_14d_complete')
    expected = [
        # u1 activated on day 14; u2's AI use on day 15 is outside the window,
        # and u2 invited without AI (milestones are not nested)
        ('2025-01-06', 'Essentials', 2, 2, 1, 2, 1, 1, True),   # billed plan in Jan, not current
        ('2025-02-24', 'Essentials', 1, 1, 0, 0, 0, 0, True),   # data_end - 14: complete
        ('2025-02-25', 'Essentials', 1, 1, 1, 1, 1, 1, False),  # data_end - 13: incomplete
        ('2025-03-10', 'Professional', 1, 0, 0, 0, 0, 0, False),
    ]
    assert got == [dict(zip(cols, e)) for e in expected]


def test_milestone_days(fx):
    _, engine, _ = fx
    got = {(r['signup_date'], r['milestone'], r['days_to_milestone']): r['users']
           for r in as_py(table(engine, 'fct_activation_milestone_days', '1, 2, 3, 4'))}
    assert got == {
        ('2025-01-06', 'placed_first_call', 0): 1, ('2025-01-06', 'placed_first_call', 1): 1,
        ('2025-01-06', 'used_ai_feature', 3): 1,
        ('2025-01-06', 'invited_team_member', 2): 1, ('2025-01-06', 'invited_team_member', 14): 1,
        ('2025-01-06', 'activated_14d', 14): 1,
        ('2025-02-24', 'placed_first_call', 0): 1,
        ('2025-02-25', 'placed_first_call', 1): 1, ('2025-02-25', 'used_ai_feature', 2): 1,
        ('2025-02-25', 'invited_team_member', 4): 1, ('2025-02-25', 'activated_14d', 4): 1,
    }


def test_revenue_monthly(fx):
    _, engine, _ = fx
    got = as_py(table(engine, 'fct_revenue_monthly', 'month_start, plan_tier'))
    cols = ('month_start', 'plan_tier', 'workspaces', 'paying_workspaces', 'billed_seats',
            'mrr_usd', 'previous_mrr_usd', 'new_mrr_usd', 'expansion_mrr_usd',
            'reactivation_mrr_usd', 'contraction_mrr_usd', 'churned_mrr_usd', 'month_complete')
    expected = [
        ('2025-01-01', 'Essentials', 1, 1, 2, 30, 0, 30, 0, 0, 0, 0, True),
        ('2025-01-01', 'Free', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, True),
        ('2025-02-01', 'Essentials', 1, 1, 1, 15, 0, 0, 0, 15, 0, 0, True),
        ('2025-02-01', 'Professional', 1, 1, 2, 50, 30, 0, 20, 0, 0, 0, True),
        ('2025-03-01', 'Essentials', 1, 0, 0, 0, 15, 0, 0, 0, 0, -15, False),
        ('2025-03-01', 'Professional', 1, 1, 1, 25, 50, 0, 0, 0, -25, 0, False),
    ]
    assert [{k: r[k] for k in cols} for r in got] == [dict(zip(cols, e)) for e in expected]
    assert [r['new_workspaces'] + r['expansion_workspaces'] + r['reactivation_workspaces']
            + r['contraction_workspaces'] + r['churned_workspaces'] for r in got] == [1, 0, 1, 1, 1, 1]


def test_nps_daily(fx):
    _, engine, _ = fx
    got = as_py(table(engine, 'fct_nps_daily', 'response_date, plan_tier'))
    assert got == [
        # 9 promoter, 8 passive (boundaries); Jan billed plan of w1 is Essentials
        {'response_date': '2025-01-31', 'plan_tier': 'Essentials', 'responses': 2,
         'promoters': 1, 'passives': 1, 'detractors': 0, 'score_sum': 17},
        # 7 passive, 6 detractor (boundaries)
        {'response_date': '2025-03-01', 'plan_tier': 'Essentials', 'responses': 2,
         'promoters': 0, 'passives': 1, 'detractors': 1, 'score_sum': 13},
        {'response_date': '2025-03-10', 'plan_tier': 'Professional', 'responses': 1,
         'promoters': 1, 'passives': 0, 'detractors': 0, 'score_sum': 10},
    ]


def test_agent_performance_daily(fx):
    _, engine, _ = fx
    got = as_py(table(engine, 'fct_agent_performance_daily', 'call_date, call_type'))
    assert got == [
        {'call_date': '2025-01-15', 'plan_tier': 'Essentials', 'call_type': 'inbound',
         'calls': 3, 'ai_resolved': 1, 'escalated': 1, 'human_handled': 1,
         'csat_sum': 6.5, 'csat_count': 2, 'handle_time_seconds_sum': 400, 'handle_time_count': 2},
        {'call_date': '2025-03-02', 'plan_tier': 'Essentials', 'call_type': 'unknown',
         'calls': 1, 'ai_resolved': 1, 'escalated': 0, 'human_handled': 0,
         'csat_sum': 5.0, 'csat_count': 1, 'handle_time_seconds_sum': 60, 'handle_time_count': 1},
    ]


def test_support_daily(fx):
    _, engine, _ = fx
    got = {(r['event_date'], r['plan_tier']): (r['active_users'], r['tickets_created'],
                                               r['tickets_resolved'])
           for r in as_py(table(engine, 'fct_support_daily', '1, 2'))}
    assert got[('2025-01-14', 'Essentials')] == (1, 1, 1)
    assert got[('2025-03-10', 'Essentials')] == (1, 1, 0)    # last day included
    assert got[('2025-03-10', 'Professional')] == (1, 0, 0)  # u5's signup, w1 in March
    assert sum(v[1] for v in got.values()) == 2 and sum(v[2] for v in got.values()) == 1
    days = sorted({d for d, _ in got})
    assert days[0] == '2025-01-06' and days[-1] == DATA_END.isoformat()


def test_monthly_usage(fx):
    _, engine, _ = fx
    activity = as_py(table(engine, 'fct_activity_monthly', 'month_start'))
    cols = ('month_start', 'active_users', 'active_workspaces', 'feature_active_users',
            'feature_active_workspaces', 'ai_active_users', 'ai_active_workspaces',
            'month_complete')
    assert activity == [dict(zip(cols, e)) for e in [
        ('2025-01-01', 2, 1, 2, 1, 2, 1, True),
        ('2025-02-01', 2, 1, 2, 1, 1, 1, True),
        ('2025-03-01', 3, 2, 1, 1, 0, 0, False),   # month not over on data_end
    ]]
    usage = {(r['month_start'], r['feature_name']): (r['active_users'], r['usage_events'])
             for r in as_py(table(engine, 'fct_feature_usage_monthly', '1, 2'))}
    assert usage == {
        ('2025-01-01', 'ai_assist'): (2, 2), ('2025-01-01', 'voice_calls'): (2, 2),
        ('2025-01-01', 'sms'): (1, 2), ('2025-02-01', 'voice_calls'): (2, 2),
        ('2025-02-01', 'ai_assist'): (1, 1), ('2025-03-01', 'sms'): (1, 1),
    }


def test_experiment_activation_curve(fx):
    _, engine, _ = fx
    got = as_py(table(engine, 'fct_experiment_activation_curve', 'variant, day_since_signup'))
    by_arm = {}
    for r in got:
        by_arm.setdefault(r['variant'], []).append(r)
    # u4 (variant_1) activated but its window is incomplete: excluded
    assert [r['users_in_window'] for r in by_arm['variant_1']] == [1] * 15
    assert [r['activated_cumulative'] for r in by_arm['variant_1']] == [0] * 14 + [1]
    assert by_arm['variant_1'][-1]['cumulative_activation_rate'] == 1.0
    assert [r['users_in_window'] for r in by_arm['variant_0']] == [2] * 15
    assert all(r['activated_cumulative'] == 0 and r['cumulative_activation_rate'] == 0
               for r in by_arm['variant_0'])


def test_feature_adoption_censoring_and_zero_denominator(fx):
    _, engine, _ = fx
    got = {(r['feature_name'], r['days_since_signup']): r
           for r in as_py(table(engine, 'fct_feature_adoption', '1, 2'))}
    # eligible users at day D: signup_date + D <= 2025-03-10
    assert [got[('voice_calls', d)]['eligible_users'] for d in (0, 1, 13, 14, 15, 63, 64, 90)] \
        == [5, 4, 4, 3, 2, 2, 0, 0]
    # ai_assist: u1 day 3, u4 day 2, u2 day 15 (u3, u5 never)
    ai14 = got[('ai_assist', 14)]
    assert ai14['cumulative_adoption_pct'] == 40.0           # 2 of all 5 users (censored)
    assert (ai14['eligible_adopters'], ai14['eligible_users']) == (1, 3)
    assert ai14['observed_adoption_pct'] == 33.33            # u1 of {u1, u2, u3}
    assert got[('ai_assist', 15)]['observed_adoption_pct'] == 100.0
    # no user has 64+ days of history: zero denominator -> NULL, never 0
    assert got[('ai_assist', 64)]['observed_adoption_pct'] is None
    assert got[('ai_assist', 90)]['cumulative_adoption_pct'] == 60.0


def test_retention_zero_cells_and_completeness(fx):
    """fct_retention_cohorts has no rows for weeks with zero active users; the
    retention functions the API will reuse must treat those complete cells as 0
    and drop incomplete ones."""
    _, engine, _ = fx
    cells = cohort_engine.load_retention(engine=engine)
    present = set(zip(pd.to_datetime(cells['cohort_week']).dt.date.astype(str),
                      cells['weeks_since_signup']))
    assert ('2025-01-06', 4) not in present                 # zero-active week: no gold row
    assert ('2025-02-24', 2) in present                     # u3's ticket on data_end
    complete = cohort_engine.as_of(cells, DATA_END)
    kept = set(zip(pd.to_datetime(complete['cohort_week']).dt.date.astype(str),
                   complete['weeks_since_signup']))
    assert ('2025-02-24', 2) not in kept                    # week 2 ends 2025-03-16
    summary = cohort_engine.summarize(complete, DATA_END)
    assert summary['cohorts'] == 2 and summary['users_in_cohorts'] == 4
    # week 1: (u1 + u4) / 4; weeks 4 and 8: only the Jan cohort is complete, 0 active
    assert summary['pooled_retention_pct'] == {'week_1': 50.0, 'week_4': 0.0, 'week_8': 0.0}


def test_changes_propagate_incrementally_and_match_full_refresh(fx):
    db, engine, tmp = fx
    with engine.begin() as conn:
        insert_events(conn, [('u3', '2025-03-09', 'ai_assist.used'),        # day 13
                             ('u3', '2025-03-10', 'support.ticket_resolved')], start=1000)
        conn.execute(text("INSERT INTO bronze.nps_responses VALUES "
                          "('n6', 'u2', 'w1', '2025-03-10', 0)"))
        conn.execute(text("UPDATE bronze.subscriptions SET billed_seats = 2, mrr_usd = 30 "
                          "WHERE workspace_id = 'w2' AND month_start = '2025-03-01'"))
    res, _ = dbt(db, tmp, 'run')    # incremental models take the new days in their window
    assert res.returncode == 0, res.stdout[-3000:]

    act = {r['signup_date']: r for r in as_py(table(engine, 'fct_activation_daily', '1'))}
    assert (act['2025-02-24']['used_ai_feature'], act['2025-02-24']['call_and_ai'],
            act['2025-02-24']['activated_14d']) == (1, 1, 0)
    support = {(r['event_date'], r['plan_tier']): r
               for r in as_py(table(engine, 'fct_support_daily', '1, 2'))}
    assert support[('2025-03-09', 'Essentials')]['active_users'] == 1
    assert support[('2025-03-10', 'Essentials')]['tickets_resolved'] == 1
    march = as_py(rows(engine, "SELECT * FROM gold.fct_activity_monthly "
                               "WHERE month_start = '2025-03-01'"))[0]
    assert (march['feature_active_users'], march['ai_active_users']) == (2, 1)
    nps = as_py(rows(engine, "SELECT * FROM gold.fct_nps_daily WHERE response_date = "
                             "'2025-03-10'"))[0]
    assert (nps['responses'], nps['promoters'], nps['detractors'], nps['score_sum']) == (2, 1, 1, 10)
    rev = as_py(rows(engine, "SELECT * FROM gold.fct_revenue_monthly WHERE month_start = "
                             "'2025-03-01' AND plan_tier = 'Essentials'"))[0]
    assert (rev['mrr_usd'], rev['expansion_mrr_usd'], rev['churned_mrr_usd']) == (30, 15, 0)
    ai13 = as_py(rows(engine, "SELECT * FROM gold.fct_feature_adoption WHERE feature_name = "
                              "'ai_assist' AND days_since_signup = 13"))[0]
    assert ai13['observed_adoption_pct'] == 75.0                # u1, u4, u3 of 4 eligible

    serving = {f'gold.{m}' for m in SERVING}
    incremental = {k: v for k, v in fingerprint(engine, schemas=('gold',)).items() if k in serving}
    res, _ = dbt(db, tmp, 'run', '--full-refresh')
    assert res.returncode == 0, res.stdout[-3000:]
    full = {k: v for k, v in fingerprint(engine, schemas=('gold',)).items() if k in serving}
    assert len(full) == len(SERVING) and incremental == full


def test_serving_tests_detect_corruption(fx):
    db, engine, tmp = fx
    with engine.begin() as conn:
        conn.execute(text("UPDATE gold.fct_activation_daily SET call_and_ai = placed_first_call + 1 "
                          "WHERE signup_date = '2025-01-06'"))
        conn.execute(text("UPDATE gold.fct_revenue_monthly SET new_mrr_usd = -5 "
                          "WHERE month_start = '2025-01-01' AND plan_tier = 'Essentials'"))
        conn.execute(text("UPDATE gold.fct_nps_daily SET promoters = promoters + 1 "
                          "WHERE response_date = '2025-01-31'"))
        conn.execute(text("UPDATE gold.fct_experiment_activation_curve SET activated_cumulative = 0 "
                          "WHERE day_since_signup = 14"))
    res, results = dbt(db, tmp, 'test', '--select', 'tag:serving')
    assert res.returncode != 0
    for test in ('test_serving_activation', 'test_serving_revenue',
                 'test_serving_nps_support_agent', 'test_serving_usage_experiments'):
        assert results[test] == 'fail', (test, results[test])
