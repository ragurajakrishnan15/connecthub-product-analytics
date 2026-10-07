"""Every data endpoint when PostgreSQL is unreachable (no database needed) and
when the warehouse is not built or holds no data (integration), plus API keys."""
import pytest
from api_testlib import (assert_problem, clean_env, client_for, warehouse_settings)  # noqa: F401
from sqlalchemy import text

from api.provision import provision
from pipeline import config

DATA_ENDPOINTS = ['/api/overview', '/api/engagement', '/api/activation', '/api/retention',
                  '/api/cohorts', '/api/revenue', '/api/feature-adoption', '/api/experiments',
                  '/api/experiments/exp_onboarding_v2', '/api/nps', '/api/support',
                  '/api/customer-health', '/api/customer-health/workspaces']


@pytest.mark.usefixtures('clean_env')
def test_every_endpoint_reports_an_unreachable_database():
    with client_for() as c:
        for path in DATA_ENDPOINTS:
            r = c.get(path)
            assert_problem(r, 503, 'database-unavailable')
            assert r.headers['retry-after'] == '5'


@pytest.mark.usefixtures('clean_env')
def test_every_endpoint_requires_the_api_key_when_enabled():
    with client_for(api_auth_mode='api_key', api_keys='s' * 32) as c:
        for path in DATA_ENDPOINTS:
            assert_problem(c.get(path), 401, 'unauthorized')
        # parameter validation still runs only for authenticated callers
        assert_problem(c.get('/api/engagement?granularity=hour'), 401, 'unauthorized')


def test_every_route_is_documented_and_tagged():
    with client_for(api_db_password='x' * 12) as c:
        spec = c.get('/api/openapi.json').json()
    documented = set(spec['paths'])
    assert {p.replace('exp_onboarding_v2', '{experiment_id}') for p in DATA_ENDPOINTS} <= documented
    for path in documented:
        op = spec['paths'][path].get('get') or spec['paths'][path]['post']
        assert op['tags'] and op['summary'] and '200' in op['responses']
        if path not in ('/api/health', '/api/health/ready'):
            assert {'401', '503'} <= set(op['responses'])


@pytest.fixture(scope='module')
def empty_db():
    settings = warehouse_settings()
    admin = config.create_engine().execution_options(isolation_level='AUTOCOMMIT')
    db = f'{settings.postgres_db}_api_states'
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    owner = config.create_engine(db)
    provision(owner, settings.api_db_user, settings.api_db_password.get_secret_value())
    yield settings.model_copy(update={'postgres_db': db}), owner
    owner.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
    admin.dispose()


@pytest.mark.integration
def test_every_endpoint_is_not_ready_before_the_warehouse_is_built(empty_db):
    settings, _ = empty_db
    with client_for(settings) as c:
        for path in DATA_ENDPOINTS:
            body = assert_problem(c.get(path), 503, 'data-not-ready')
            assert 'pipeline' in body['detail']
        # an unknown experiment is still a 404: the registry is checked first
        assert_problem(c.get('/api/experiments/exp_unknown'), 404, 'experiment-not-found')


@pytest.mark.integration
def test_tables_without_data_are_not_ready(empty_db):
    settings, owner = empty_db
    with owner.begin() as conn:
        conn.execute(text('CREATE TABLE gold.fct_daily_active_users (event_date date, dau bigint, '
                          'active_workspaces bigint, wau_7d numeric, mau_28d numeric)'))
    with client_for(settings) as c:
        for path in DATA_ENDPOINTS:
            body = assert_problem(c.get(path), 503, 'data-not-ready')
            assert 'no data loaded' in body['detail'] or 'run the pipeline' in body['detail']
