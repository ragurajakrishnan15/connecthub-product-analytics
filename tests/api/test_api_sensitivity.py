"""Integration: API responses follow the warehouse data.

A separate warehouse (<POSTGRES_DB>_api_sensitivity) is built by the real
pipeline (1,500 users). The test records API responses, changes the bronze
data in targeted ways, rebuilds with dbt (full refresh) and the analytics
step, and requires every response to change by exactly the expected amount:
a fixed or cached value cannot pass. Then it removes a persisted experiment
result and empties a table to check those states. Takes ~2-3 minutes.
"""
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from api_testlib import assert_problem, client_for, warehouse_settings
from sqlalchemy import text

from api.provision import provision
from pipeline import config

pytestmark = pytest.mark.integration
USERS = 1500
ADDED_DETRACTORS, ADDED_TICKETS, ADDED_MRR, NEWLY_ACTIVE, NEWLY_ACTIVATED = 20, 50, 1000, 5, 25


def run(cmd, env, timeout=1200):
    res = subprocess.run(cmd, cwd=config.PROJECT_ROOT, env=env, capture_output=True, text=True,
                         timeout=timeout)
    assert res.returncode == 0, (res.stdout + res.stderr)[-3000:]
    return res


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    settings = warehouse_settings()
    admin = config.create_engine().execution_options(isolation_level='AUTOCOMMIT')
    db = f'{settings.postgres_db}_api_sensitivity'
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    tmp = tmp_path_factory.mktemp('api_sensitivity')
    env = dict(os.environ, POSTGRES_HOST=os.environ.get('POSTGRES_HOST', '127.0.0.1'))
    run([sys.executable, '-m', 'pipeline', 'run', '--users', str(USERS), '--database', db,
         '--data-dir', str(tmp / 'data'), '--run-id', 'api-sensitivity'], env)
    owner = config.create_engine(db)
    provision(owner, settings.api_db_user, settings.api_db_password.get_secret_value())
    yield settings.model_copy(update={'postgres_db': db}), owner, db, tmp, env
    owner.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
    admin.dispose()


def snapshot(c, workspace_id):
    def ok(path, **params):
        r = c.get(path, params=params)
        assert r.status_code == 200, r.text
        return r.json()['data']
    pages, offset = [], 0
    while offset is not None:
        page = ok('/api/customer-health/workspaces', limit=100, offset=offset)
        pages += page['items']
        offset = page['next_offset']
    return {
        'overview': ok('/api/overview'), 'nps': ok('/api/nps'), 'support': ok('/api/support'),
        'revenue': ok('/api/revenue'), 'engagement': ok('/api/engagement'),
        'experiment': ok('/api/experiments/exp_onboarding_v2'),
        'workspace': next(w for w in pages if w['workspace_id'] == workspace_id),
    }


def test_responses_change_exactly_with_the_data(built):
    settings, owner, db, tmp, env = built
    with owner.connect() as conn:
        data_end = conn.execute(text('SELECT MAX(event_date) FROM staging.stg_events')).scalar()
        # a user active on the last day (so new events that day do not change DAU)
        user, ws = conn.execute(text(
            'SELECT user_id, workspace_id FROM staging.stg_events WHERE event_date = :d '
            'ORDER BY user_id LIMIT 1'), {'d': data_end}).one()
        inactive = conn.execute(text(
            'SELECT u.user_id, u.workspace_id FROM staging.stg_users u WHERE NOT EXISTS ('
            'SELECT 1 FROM staging.stg_events e WHERE e.user_id = u.user_id AND e.event_date = :d) '
            'ORDER BY u.user_id LIMIT :n'), {'d': data_end, 'n': NEWLY_ACTIVE}).all()
        control = conn.execute(text("""
            SELECT m.user_id, u.workspace_id, m.signup_date FROM gold.fct_experiment_user_metrics m
            JOIN staging.stg_users u USING (user_id)
            WHERE m.experiment_id = 'exp_onboarding_v2' AND m.variant = 'variant_0'
              AND m.window_14d_complete AND m.activated_14d = 0
            ORDER BY m.user_id LIMIT :n"""), {'n': NEWLY_ACTIVATED}).all()
        arm = conn.execute(text("""
            SELECT COUNT(*), SUM(activated_14d) FROM gold.fct_experiment_user_metrics
            WHERE experiment_id = 'exp_onboarding_v2' AND variant = 'variant_0'
              AND window_14d_complete""")).one()
    assert len(inactive) == NEWLY_ACTIVE and len(control) == NEWLY_ACTIVATED

    with client_for(settings) as c:
        before = snapshot(c, ws)

    def event(i, u, w, name, d):
        return {'id': f'sens-{i}', 'u': u, 'w': w, 'n': name, 'ts': f'{d} 12:00:00', 'd': d,
                's': f'sens-{u}-{d}'}
    events = [event(i, user, ws, 'support.ticket_created', data_end) for i in range(ADDED_TICKETS)]
    events += [event(100 + i, u, w, 'sms.sent', data_end) for i, (u, w) in enumerate(inactive)]
    for i, (u, w, signup) in enumerate(control):
        day = signup + timedelta(days=1)
        events += [event(200 + 3 * i + k, u, w, name, day) for k, name in enumerate(
            ('call.started', 'ai_assist.used', 'team.member_invited'))]
    with owner.begin() as conn:
        conn.execute(text(
            'INSERT INTO bronze.events_raw (event_id, user_id, workspace_id, event_name, '
            "timestamp_utc, event_date, session_id, platform, country_code) "
            "VALUES (:id, :u, :w, :n, :ts, :d, :s, 'web', 'US')"), events)
        conn.execute(text("INSERT INTO bronze.nps_responses VALUES (:i, :u, :w, :d, 0)"),
                     [{'i': f'sens-nps-{i}', 'u': user, 'w': ws, 'd': data_end}
                      for i in range(ADDED_DETRACTORS)])
        conn.execute(text('UPDATE bronze.subscriptions SET mrr_usd = mrr_usd + :x '
                          "WHERE workspace_id = :w AND month_start = DATE_TRUNC('month', CAST(:d AS date))"),
                     {'x': ADDED_MRR, 'w': ws, 'd': data_end})

    dbt = os.path.join(os.path.dirname(sys.executable), 'dbt.exe' if os.name == 'nt' else 'dbt')
    run([dbt, 'run', '--full-refresh', '--project-dir', config.dbt_dir(), '--profiles-dir',
         config.dbt_dir()], dict(env, POSTGRES_DB=db, DBT_TARGET_PATH=str(tmp / 'target'),
                                 DBT_LOG_PATH=str(tmp / 'logs')))
    run([sys.executable, '-m', 'pipeline', 'step', 'analytics', '--database', db,
         '--data-dir', str(tmp / 'data')], env)

    with client_for(settings) as c:
        after = snapshot(c, ws)

    b, a = before, after
    assert a['nps']['summary']['responses'] == b['nps']['summary']['responses'] + ADDED_DETRACTORS
    assert a['nps']['summary']['detractors'] == b['nps']['summary']['detractors'] + ADDED_DETRACTORS
    assert a['nps']['summary']['nps'] < b['nps']['summary']['nps']
    assert a['support']['tickets']['created'] == b['support']['tickets']['created'] + ADDED_TICKETS
    assert a['revenue']['latest']['mrr_usd'] == pytest.approx(
        b['revenue']['latest']['mrr_usd'] + ADDED_MRR)
    assert a['overview']['kpis']['mrr_usd']['value'] == pytest.approx(
        b['overview']['kpis']['mrr_usd']['value'] + ADDED_MRR)
    assert a['engagement']['summary']['dau'] == b['engagement']['summary']['dau'] + NEWLY_ACTIVE
    assert a['overview']['kpis']['dau']['value'] == b['overview']['kpis']['dau']['value'] + NEWLY_ACTIVE
    n, activated = arm
    assert a['experiment']['evaluation']['primary']['control_rate'] == \
        round((activated + NEWLY_ACTIVATED) / n, 4) != b['experiment']['evaluation']['primary'][
            'control_rate']
    curve = next(x for x in a['experiment']['activation_curve'] if x['variant'] == 'variant_0')
    assert curve['points'][-1]['activated'] == activated + NEWLY_ACTIVATED
    assert a['workspace']['support_tickets_last_30d'] == \
        b['workspace']['support_tickets_last_30d'] + ADDED_TICKETS
    assert a['workspace']['nps_score'] != b['workspace']['nps_score']


def test_missing_evaluation_and_empty_tables(built):
    settings, owner, *_ = built
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM analytics.experiment_results "
                          "WHERE experiment_id = 'exp_ai_summary_v1'"))
    with client_for(settings) as c:
        detail = c.get('/api/experiments/exp_ai_summary_v1').json()['data']
        assert detail['status'] == 'not_evaluated' and detail['evaluation'] is None
        listed = {e['experiment']['experiment_id']: e for e in
                  c.get('/api/experiments').json()['data']['experiments']}
        assert listed['exp_ai_summary_v1']['status'] == 'not_evaluated'
        assert listed['exp_ai_summary_v1']['primary'] is None
        pending = c.get('/api/experiments', params={'status': 'not_evaluated'}).json()['data']
        assert [e['experiment']['experiment_id'] for e in pending['experiments']] == \
            ['exp_ai_summary_v1']
        assert c.get('/api/overview').json()['data']['experiments']['evaluated'] == 1
    with owner.begin() as conn:
        conn.execute(text('TRUNCATE analytics.workspace_health_scores'))
    with client_for(settings) as c:
        assert_problem(c.get('/api/customer-health'), 503, 'data-not-ready')
        assert_problem(c.get('/api/customer-health/workspaces'), 503, 'data-not-ready')
        overview = c.get('/api/overview')
        assert overview.status_code == 200
        assert overview.json()['data']['health']['workspaces'] == 0
