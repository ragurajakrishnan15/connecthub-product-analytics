"""Integration: the API against real PostgreSQL databases.

- the built local warehouse: readiness 200, /api/meta equals the warehouse
- an empty database (warehouse not built): readiness 503, /api/meta 503
- the read-only role: can read gold/analytics/ops.pipeline_runs only, cannot
  write, has its timeouts, and keeps its grants across dbt rebuilds

Needs POSTGRES_* and API_DB_PASSWORD; the role is (re)provisioned first.
"""
import os
import subprocess
import sys

import pytest
from api_testlib import assert_problem, client_for, warehouse_settings
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from api.db import engine_url
from api.provision import provision
from pipeline import config

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module')
def settings():
    s = warehouse_settings()
    owner = config.create_engine(s.postgres_db)
    try:
        with owner.connect() as conn:
            conn.execute(text('SELECT 1 FROM gold.fct_activation_daily LIMIT 1'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built: {exc}')
    provision(owner, s.api_db_user, s.api_db_password.get_secret_value(),
              s.api_statement_timeout_ms)
    owner.dispose()
    return s


@pytest.fixture(scope='module')
def owner_engine(settings):
    engine = config.create_engine(settings.postgres_db)
    yield engine
    engine.dispose()


@pytest.fixture(scope='module')
def role_engine(settings):
    """Connects as the API role with only its role defaults (no session options)."""
    engine = sa_create_engine(engine_url(settings))
    yield engine
    engine.dispose()


# ---------------------------------------------------------------- built warehouse
def test_ready_on_the_built_warehouse(settings, owner_engine):
    with client_for(settings) as c:
        r = c.get('/api/health/ready')
    assert r.status_code == 200, r.text
    body = r.json()
    with owner_engine.connect() as conn:
        last_run = conn.execute(text(
            "SELECT run_id FROM ops.pipeline_runs WHERE step = 'validate_analytics' "
            "AND status = 'success' ORDER BY started_at DESC LIMIT 1")).scalar()
        data_end = conn.execute(text('SELECT MAX(event_date) FROM gold.fct_daily_active_users')
                                ).scalar()
    assert body['status'] == 'ready'
    assert body['checks']['relations'] == {'ok': True, 'required': 17, 'missing': [],
                                           'not_readable': []}
    assert body['checks']['pipeline']['last_success_run_id'] == last_run
    assert body['data_end'] == data_end.isoformat()
    assert body['checks']['database']['ok'] and body['checks']['database']['latency_ms'] >= 0


def test_meta_matches_the_warehouse(settings, owner_engine):
    with client_for(settings) as c:
        r = c.get('/api/meta')
    assert r.status_code == 200, r.text
    data, meta = r.json()['data'], r.json()['meta']
    with owner_engine.connect() as conn:
        start, end = conn.execute(text(
            'SELECT MIN(event_date), MAX(event_date) FROM gold.fct_daily_active_users')).one()
        snapshot = conn.execute(text('SELECT MAX(snapshot_date) FROM gold.metrics_product_health')
                                ).scalar()
        tiers = conn.execute(text(
            'SELECT plan_tier, MIN(seat_price_usd) FROM gold.fct_workspace_mrr '
            'GROUP BY 1 ORDER BY 2, 1')).all()
        run = conn.execute(text(
            "SELECT run_id FROM ops.pipeline_runs WHERE step = 'validate_analytics' "
            "AND status = 'success' ORDER BY started_at DESC LIMIT 1")).scalar()
        run_steps = conn.execute(text(
            'SELECT COUNT(*), SUM(duration_s) FROM ops.pipeline_runs WHERE run_id = :r'),
            {'r': run}).one()
    assert (data['data_start'], data['data_end']) == (start.isoformat(), end.isoformat())
    assert data['health_snapshot_date'] == snapshot.isoformat()
    assert [(t['name'], t['seat_price_usd']) for t in data['plan_tiers']] == \
        [(name, float(price)) for name, price in tiers]
    assert data['last_successful_run']['run_id'] == run == meta['data_version']
    assert run_steps[0] == 8
    assert data['last_successful_run']['duration_s'] == pytest.approx(float(run_steps[1]))
    assert {e['experiment_id']: e['kind'] for e in data['experiments']} == \
        {'exp_onboarding_v2': 'ab', 'exp_ai_summary_v1': 'aa'}
    assert [t['label'] for t in data['health_tiers']] == ['Critical', 'At Risk', 'Healthy',
                                                          'Champion']
    assert {f['name'] for f in data['features'] if f['is_ai']} == {'ai_assist', 'ai_voice_agent'}
    assert meta['as_of'] == end.isoformat() and meta['sources']
    assert data['dataset'] == {'label': 'synthetic', 'synthetic': True} and meta['caveats']


def test_meta_logs_its_database_queries(settings):
    from api_testlib import capture_logs
    with client_for(settings) as c:
        logs = capture_logs()
        c.get('/api/meta')
    line = [entry for entry in logs() if entry['event'] == 'request'][-1]
    assert line['status'] == 200 and line['db_queries'] >= 4 and line['db_ms'] > 0


def test_app_sessions_are_read_only_with_timeouts(settings):
    with client_for(settings) as c:
        engine = c.app.state.engine
        with engine.connect() as conn:
            shown = {name: conn.execute(text(f'SHOW {name}')).scalar() for name in
                     ('default_transaction_read_only', 'statement_timeout', 'lock_timeout',
                      'application_name')}
            assert shown == {'default_transaction_read_only': 'on', 'statement_timeout': '5s',
                             'lock_timeout': '2s', 'application_name': 'connecthub-api'}
            with pytest.raises(Exception, match='read-only transaction'):
                conn.execute(text('CREATE TEMP TABLE api_probe (x int)'))


# ---------------------------------------------------------------- read-only role
def test_role_reads_only_what_the_api_needs(role_engine):
    with role_engine.connect() as conn:
        for rel in ('gold.fct_revenue_monthly', 'analytics.experiment_results',
                    'ops.pipeline_runs', 'gold.fct_daily_active_users'):
            conn.execute(text(f'SELECT 1 FROM {rel} LIMIT 1'))
    for rel in ('bronze.events_raw', 'staging.stg_users', 'intermediate.int_sessions',
                'experiments.experiment_assignments', 'ops.load_state'):
        with role_engine.connect() as conn:
            with pytest.raises(Exception, match='permission denied'):
                conn.execute(text(f'SELECT 1 FROM {rel} LIMIT 1'))


def test_role_is_read_only_by_default(role_engine):
    with role_engine.connect() as conn:
        with pytest.raises(Exception, match='read-only transaction'):
            conn.execute(text('UPDATE gold.fct_revenue_monthly SET mrr_usd = 0'))


def test_role_cannot_write_even_in_a_read_write_transaction(role_engine):
    """Privileges alone block writes: the second layer behind read-only mode."""
    statements = ["UPDATE gold.fct_revenue_monthly SET mrr_usd = 0",
                  'CREATE TABLE gold.api_probe (x int)',
                  "INSERT INTO ops.pipeline_runs (run_id, step, status, started_at) "
                  "VALUES ('x', 'y', 'z', now())"]
    for stmt in statements:
        with role_engine.connect() as conn:
            # first statement of the transaction, so it overrides the role default
            conn.execute(text('SET TRANSACTION READ WRITE'))
            with pytest.raises(Exception, match='permission denied'):
                conn.execute(text(stmt))


def test_role_defaults_and_attributes(role_engine, owner_engine, settings):
    with role_engine.connect() as conn:
        assert conn.execute(text('SHOW default_transaction_read_only')).scalar() == 'on'
        assert conn.execute(text('SHOW statement_timeout')).scalar() == '5s'
    with owner_engine.connect() as conn:
        attrs = conn.execute(text(
            'SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, '
            'rolcanlogin FROM pg_roles WHERE rolname = :u'), {'u': settings.api_db_user}).one()
    assert tuple(attrs) == (False, False, False, False, False, True)


def test_grants_survive_dbt_rebuilds(settings, role_engine, tmp_path):
    """dbt replaces tables (swap, full refresh); default privileges keep them readable."""
    exe = os.path.join(os.path.dirname(sys.executable), 'dbt.exe' if os.name == 'nt' else 'dbt')
    env = dict(os.environ, DBT_TARGET_PATH=str(tmp_path), DBT_LOG_PATH=str(tmp_path),
               POSTGRES_HOST=os.environ.get('POSTGRES_HOST', '127.0.0.1'))
    with role_engine.connect() as conn:
        before = conn.execute(text('SELECT COUNT(*) FROM gold.fct_revenue_monthly')).scalar()
    res = subprocess.run([exe, 'run', '--select', 'fct_revenue_monthly', 'fct_activity_monthly',
                          '--full-refresh', '--project-dir', config.dbt_dir(),
                          '--profiles-dir', config.dbt_dir()],
                         env=env, capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, res.stdout[-2000:]
    with role_engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM gold.fct_revenue_monthly')).scalar() == before
        conn.execute(text('SELECT 1 FROM gold.fct_activity_monthly LIMIT 1'))


def test_provisioning_is_idempotent(settings, owner_engine):
    first = provision(owner_engine, settings.api_db_user,
                      settings.api_db_password.get_secret_value())
    second = provision(owner_engine, settings.api_db_user,
                       settings.api_db_password.get_secret_value())
    assert first['action'] == second['action'] == 'updated'
    assert first['readable_tables'] == second['readable_tables'] >= 17
    with pytest.raises(ValueError, match='must differ from the warehouse owner'):
        provision(owner_engine, os.environ.get('POSTGRES_USER', 'connecthub'), 'whatever-123')


# ---------------------------------------------------------------- warehouse not built
@pytest.fixture(scope='module')
def empty_db(settings):
    admin = config.create_engine().execution_options(isolation_level='AUTOCOMMIT')
    db = f'{settings.postgres_db}_api_empty'
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    owner = config.create_engine(db)
    provision(owner, settings.api_db_user, settings.api_db_password.get_secret_value())
    yield db, owner
    owner.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
    admin.dispose()


def test_not_built_warehouse_is_not_ready(settings, empty_db):
    db, _ = empty_db
    with client_for(settings.model_copy(update={'postgres_db': db})) as c:
        ready = c.get('/api/health/ready')
        meta = c.get('/api/meta')
    assert ready.status_code == 503
    body = ready.json()
    assert body['status'] == 'not_ready' and body['checks']['database']['ok'] is True
    relations = body['checks']['relations']
    # provisioning creates ops.pipeline_runs; everything the pipeline builds is missing
    assert relations['ok'] is False and 'ops.pipeline_runs' not in relations['missing']
    assert len(relations['missing']) == 16 and 'gold.fct_activation_daily' in relations['missing']
    assert body['checks']['pipeline'] == {'ok': False, 'last_success_run_id': None,
                                          'last_success_at': None}
    assert body['data_end'] is None
    problem = assert_problem(meta, 503, 'data-not-ready')
    assert 'run the pipeline' in problem['detail']


def test_unreadable_relation_is_reported_not_raised(settings, empty_db):
    db, owner = empty_db
    with owner.begin() as conn:
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS gold'))
        conn.execute(text('CREATE TABLE gold.fct_daily_active_users (event_date date)'))
        conn.execute(text(f'REVOKE SELECT ON gold.fct_daily_active_users FROM {settings.api_db_user}'))
    with client_for(settings.model_copy(update={'postgres_db': db})) as c:
        body = c.get('/api/health/ready').json()
        meta = c.get('/api/meta')
    assert 'gold.fct_daily_active_users' in body['checks']['relations']['not_readable']
    problem = assert_problem(meta, 503, 'data-not-ready')
    assert 'api.provision' in problem['detail']
