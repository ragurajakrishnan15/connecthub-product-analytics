"""HTTP behaviour without a database: liveness, readiness and /api/meta when
PostgreSQL is unreachable, problem documents, request IDs, logging, security
headers, CORS, API keys, docs and the OpenAPI document."""
import pytest
from api_testlib import (FAKE_PASSWORD, assert_problem, capture_logs, clean_env,  # noqa: F401
                         client_for, make_settings)
from sqlalchemy import exc as sa_exc

from api.errors import classify_db_error

pytestmark = pytest.mark.usefixtures('clean_env')


@pytest.fixture
def client():
    with client_for() as c:
        yield c


# ---------------------------------------------------------------- health
def test_liveness_needs_no_database(client):
    r = client.get('/api/health')
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'ok' and body['version'] == '0.1.0' and body['uptime_s'] >= 0
    assert r.headers['cache-control'] == 'no-store'


def test_head_is_served_without_a_body(client):
    r = client.head('/api/health')
    assert r.status_code == 200 and r.content == b''
    assert r.headers['x-request-id']


def test_readiness_reports_an_unreachable_database_as_503(client):
    r = client.get('/api/health/ready')
    assert r.status_code == 503
    body = r.json()
    assert body['status'] == 'not_ready'
    assert body['checks']['database'] == {'ok': False, 'latency_ms': None,
                                          'error': 'database-unavailable'}
    assert body['checks']['relations'] is None and body['checks']['pipeline'] is None
    assert r.headers['cache-control'] == 'no-store'


def test_meta_with_an_unreachable_database_is_a_retryable_problem(client):
    r = client.get('/api/meta')
    body = assert_problem(r, 503, 'database-unavailable')
    assert r.headers['retry-after'] == '5'
    assert FAKE_PASSWORD not in r.text and 'psycopg2' not in r.text and 'SELECT' not in r.text
    assert body['instance'] == '/api/meta'


# ---------------------------------------------------------------- errors
def test_unknown_route_is_a_404_problem(client):
    assert_problem(client.get('/api/does-not-exist'), 404, 'not-found')


def test_wrong_method_is_a_405_problem(client):
    assert_problem(client.post('/api/health'), 405, 'method-not-allowed')


def test_unknown_query_parameter_is_rejected(client):
    body = assert_problem(client.get('/api/meta?plan=Free'), 400, 'invalid-parameter')
    assert body['errors'] == [{'loc': ['query', 'plan'], 'msg': 'unknown parameter',
                               'type': 'unknown_parameter'}]
    assert_problem(client.get('/api/health?x=1'), 400, 'invalid-parameter')


def test_overlong_query_string_is_a_414_problem(client):
    assert_problem(client.get('/api/health?' + 'a' * 3000), 414, 'uri-too-long')


def test_unhandled_exception_is_a_generic_500_and_logged_redacted(monkeypatch):
    monkeypatch.setenv('API_DB_PASSWORD', FAKE_PASSWORD)    # what redact() masks
    app_client = client_for()
    app = app_client.app

    @app.get('/api/_boom')
    def boom():
        raise RuntimeError(f'exploded with {FAKE_PASSWORD}')

    with app_client as c:
        logs = capture_logs()
        r = c.get('/api/_boom')
    body = assert_problem(r, 500, 'internal-error')
    assert 'exploded' not in r.text and FAKE_PASSWORD not in r.text
    assert body['detail'] == 'an unexpected error occurred'
    errors = [line for line in logs() if line['event'] == 'unhandled_error']
    assert len(errors) == 1 and errors[0]['request_id'] == r.headers['x-request-id']
    assert 'exploded with ***' in errors[0]['error'] and FAKE_PASSWORD not in errors[0]['error']


class _PgError(Exception):
    def __init__(self, pgcode):
        super().__init__('boom')
        self.pgcode = pgcode


@pytest.mark.parametrize('error,slug', [
    (sa_exc.ProgrammingError('SELECT 1', {}, _PgError('42P01')), 'data-not-ready'),
    (sa_exc.ProgrammingError('SELECT 1', {}, _PgError('42501')), 'data-not-ready'),
    (sa_exc.OperationalError('SELECT 1', {}, _PgError('57014')), 'query-timeout'),
    (sa_exc.OperationalError('SELECT 1', {}, _PgError('55P03')), 'warehouse-busy'),
    (sa_exc.OperationalError('connect', {}, _PgError(None)), 'database-unavailable'),
    (sa_exc.TimeoutError('pool'), 'pool-exhausted'),
    (sa_exc.ProgrammingError('SELECT 1', {}, _PgError('42601')), 'internal-error'),
])
def test_database_errors_map_to_problem_slugs(error, slug):
    assert classify_db_error(error)[0] == slug


def test_database_error_responses_carry_status_and_retry_after():
    app_client = client_for()

    @app_client.app.get('/api/_timeout')
    def timeout():
        raise sa_exc.OperationalError('SELECT pg_sleep(9)', {}, _PgError('57014'))

    @app_client.app.get('/api/_missing')
    def missing():
        raise sa_exc.ProgrammingError('SELECT * FROM gold.x', {}, _PgError('42P01'))

    with app_client as c:
        r = c.get('/api/_timeout')
        assert_problem(r, 504, 'query-timeout')
        assert 'retry-after' not in r.headers and 'pg_sleep' not in r.text
        r = c.get('/api/_missing')
        assert_problem(r, 503, 'data-not-ready')
        assert r.headers['retry-after'] == '5' and 'gold.x' not in r.text


# ---------------------------------------------------------------- request IDs and logging
def test_request_ids_are_generated_or_propagated(client):
    generated = client.get('/api/health').headers['x-request-id']
    assert len(generated) == 32 and int(generated, 16) >= 0
    assert client.get('/api/health', headers={'X-Request-ID': 'trace-1234-abcd'}) \
        .headers['x-request-id'] == 'trace-1234-abcd'
    for bad in ('short', 'has spaces in it', 'x' * 65, 'semi;colon-12345'):
        rid = client.get('/api/health', headers={'X-Request-ID': bad}).headers['x-request-id']
        assert rid != bad and len(rid) == 32


def test_one_json_log_line_per_request():
    with client_for() as c:
        logs = capture_logs()
        r = c.get('/api/health', headers={'X-Request-ID': 'log-test-0001'})
        c.get('/api/meta')
    lines = [line for line in logs() if line['event'] == 'request']
    assert len(lines) == 2
    first = lines[0]
    assert first['request_id'] == r.headers['x-request-id'] == 'log-test-0001'
    assert (first['method'], first['route'], first['status']) == ('GET', '/api/health', 200)
    assert first['duration_ms'] >= 0 and first['db_queries'] == 0
    assert lines[1]['route'] == '/api/meta' and lines[1]['status'] == 503


# ---------------------------------------------------------------- security headers
@pytest.mark.parametrize('method,path', [('get', '/api/health'), ('get', '/api/nope'),
                                         ('post', '/api/health'), ('get', '/api/meta')])
def test_security_headers_on_every_response(client, method, path):
    r = getattr(client, method)(path)
    assert r.headers['x-content-type-options'] == 'nosniff'
    assert r.headers['referrer-policy'] == 'no-referrer'
    assert r.headers['x-frame-options'] == 'DENY'
    assert r.headers['content-security-policy'] == "default-src 'none'; frame-ancestors 'none'"
    assert 'server' not in {k.lower() for k in r.headers}


def test_docs_get_a_csp_that_allows_their_assets(client):
    r = client.get('/api/docs')
    assert r.status_code == 200
    assert 'https://cdn.jsdelivr.net' in r.headers['content-security-policy']


# ---------------------------------------------------------------- CORS
def test_cors_allows_only_configured_origins(client):
    ok = client.get('/api/health', headers={'Origin': 'http://localhost:8000'})
    assert ok.headers['access-control-allow-origin'] == 'http://localhost:8000'
    assert 'access-control-allow-credentials' not in ok.headers
    assert 'x-request-id' in ok.headers['access-control-expose-headers'].lower()
    for origin in ('http://evil.example.com', 'null'):
        denied = client.get('/api/health', headers={'Origin': origin})
        assert 'access-control-allow-origin' not in denied.headers


def test_cors_preflight_allows_get_but_not_writes(client):
    headers = {'Origin': 'http://127.0.0.1:8000', 'Access-Control-Request-Method': 'GET',
               'Access-Control-Request-Headers': 'X-API-Key'}
    pre = client.options('/api/meta', headers=headers)
    assert pre.status_code == 200
    assert pre.headers['access-control-allow-origin'] == 'http://127.0.0.1:8000'
    assert 'POST' not in pre.headers['access-control-allow-methods']
    post = client.options('/api/meta', headers={**headers, 'Access-Control-Request-Method': 'POST'})
    assert post.status_code == 400


# ---------------------------------------------------------------- API keys
def test_api_key_mode_protects_meta_but_not_health():
    key = 'k' * 32
    with client_for(api_auth_mode='api_key', api_keys=key) as c:
        assert c.get('/api/health').status_code == 200
        assert c.get('/api/health/ready').status_code == 503      # open; DB is down
        r = c.get('/api/meta')
        assert_problem(r, 401, 'unauthorized')
        assert r.headers['www-authenticate'] == 'ApiKey header="X-API-Key"'
        assert_problem(c.get('/api/meta', headers={'X-API-Key': 'wrong' * 7}), 401, 'unauthorized')
        # a valid key passes authentication and reaches the (unreachable) database
        assert_problem(c.get('/api/meta', headers={'X-API-Key': key}), 503, 'database-unavailable')


# ---------------------------------------------------------------- docs and OpenAPI
def test_openapi_documents_exactly_the_implemented_endpoints(client):
    spec = client.get('/api/openapi.json').json()
    assert spec['info']['title'] == 'ConnectHub Analytics API' and spec['info']['version'] == '0.1.0'
    assert set(spec['paths']) == {
        '/api/health', '/api/health/ready', '/api/meta',                       # Phase 4B
        '/api/overview', '/api/engagement', '/api/activation', '/api/retention',  # Phase 4C
        '/api/cohorts', '/api/revenue', '/api/feature-adoption', '/api/experiments',
        '/api/experiments/{experiment_id}', '/api/nps', '/api/support', '/api/customer-health',
        '/api/customer-health/workspaces'}
    assert all(set(ops) == {'get'} for ops in spec['paths'].values())     # read-only surface
    for path, ops in spec['paths'].items():
        for op in ops.values():
            assert op['tags'] and op['summary'], path
    meta = spec['paths']['/api/meta']['get']['responses']
    assert {'200', '400', '401', '503', '504'} <= set(meta)
    assert 'application/problem+json' in meta['503']['content']
    assert spec['paths']['/api/health/ready']['get']['responses']['503']['content'][
        'application/json']['schema']['$ref'].endswith('/Readiness')


def test_docs_are_disabled_in_production():
    with client_for(api_env='production', api_auth_mode='api_key', api_keys='z' * 32) as c:
        for path in ('/api/docs', '/api/redoc', '/api/openapi.json'):
            assert_problem(c.get(path), 404, 'not-found')
        assert c.get('/api/health').status_code == 200
