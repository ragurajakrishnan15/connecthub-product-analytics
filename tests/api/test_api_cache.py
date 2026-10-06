"""Response cache, data-version keying, ETags and conditional requests (api/cache.py).

Unit tests need no database; the integration tests run against the built
local warehouse (read-only). The data_version change caused by a real pipeline
run is covered in test_api_sensitivity.py."""
import asyncio
import gzip
import json
import os
import re

import pytest
from pydantic import SecretStr
from api_testlib import (FAKE_PASSWORD, assert_problem, capture_logs, clean_env,  # noqa: F401
                         client_for, make_settings, warehouse_settings)
from sqlalchemy import text

from api.cache import (CACHEABLE_PATHS, DataVersion, ResponseCache, cache_key, etag_for,
                       etag_matches, is_cacheable)
from api.provision import provision
from pipeline import config

ETAG = re.compile(r'^W/"[0-9a-f]{32}"$')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def body(meta=None, **data):
    return json.dumps({'data': data, 'meta': {'generated_at': '2026-01-01T00:00:00Z',
                                              'data_version': 'v1', **(meta or {})}}).encode()


# ================================================================ unit: keys and ETags
def test_cache_key_covers_path_all_parameters_and_version():
    k = cache_key('/api/nps', 'start=2025-01-01&end=2025-02-01', 'run-1')
    assert k == cache_key('/api/nps', 'end=2025-02-01&start=2025-01-01', 'run-1')   # order-free
    assert k != cache_key('/api/nps', 'start=2025-01-01&end=2025-02-02', 'run-1')
    assert k != cache_key('/api/support', 'start=2025-01-01&end=2025-02-01', 'run-1')
    assert k != cache_key('/api/nps', 'start=2025-01-01&end=2025-02-01', 'run-2')
    assert cache_key('/a', 'f=x&f=y', 'v') != cache_key('/a', 'f=x', 'v')            # repeats kept
    assert cache_key('/a', 'x=', 'v') != cache_key('/a', '', 'v')


def test_etag_is_deterministic_content_based_and_ignores_generated_at():
    a = etag_for(body(nps=8.4))
    assert ETAG.match(a)
    assert a == etag_for(body(nps=8.4))
    assert a == etag_for(body({'generated_at': '2027-05-05T10:00:00Z'}, nps=8.4))
    assert a != etag_for(body(nps=8.5))
    assert a != etag_for(body({'data_version': 'v2'}, nps=8.4))
    # canonical: key order and whitespace do not matter
    reordered = json.dumps({'meta': {'data_version': 'v1', 'generated_at': 'x'},
                            'data': {'nps': 8.4}}, indent=2).encode()
    assert a == etag_for(reordered)


def test_if_none_match_uses_weak_comparison():
    tag = 'W/"abc"'
    assert etag_matches('W/"abc"', tag) and etag_matches('"abc"', tag)
    assert etag_matches('W/"zzz", W/"abc"', tag) and etag_matches('*', tag)
    assert not etag_matches('W/"abd"', tag) and not etag_matches('', tag)
    assert not etag_matches(None, tag)


def test_ttl_expiry_lru_and_byte_bounds():
    clock = Clock()
    cache = ResponseCache(ttl_s=10, max_entries=2, max_bytes=10_000, clock=clock)
    cache.put('a', b'x' * 10, 'W/"a"', 'application/json')
    cache.put('b', b'x' * 10, 'W/"b"', 'application/json')
    assert cache.get('a') is not None               # 'a' is now most recently used
    cache.put('c', b'x' * 10, 'W/"c"', 'application/json')
    assert cache.keys() == ['a', 'c'] and cache.evictions == 1        # 'b' evicted (LRU)
    clock.now += 10.01
    assert cache.get('a') is None and len(cache) == 1                # expired and dropped
    small = ResponseCache(ttl_s=10, max_entries=100, max_bytes=25, clock=clock)
    for k in 'pqr':
        small.put(k, b'y' * 10, '', 'application/json')
    assert small.keys() == ['q', 'r'] and small.bytes == 20            # byte budget
    assert small.put('huge', b'z' * 26, '', 'application/json') is False and 'huge' not in small.keys()


def test_data_version_is_reread_only_after_its_ttl(monkeypatch):
    clock = Clock()
    dv = DataVersion(ttl_s=30, clock=clock)
    versions = iter(['run-1', 'run-2'])
    monkeypatch.setattr(dv, '_read', lambda engine: next(versions))
    assert asyncio.run(dv.get(None)) == 'run-1'
    clock.now += 29
    assert asyncio.run(dv.get(None)) == 'run-1'
    clock.now += 2
    assert asyncio.run(dv.get(None)) == 'run-2'

    failing = DataVersion(ttl_s=30, clock=clock)

    def boom(engine):
        raise RuntimeError(f'cannot connect with {FAKE_PASSWORD}')
    monkeypatch.setattr(failing, '_read', boom)
    assert asyncio.run(failing.get(None)) is None and failing.expires == float('-inf')


def test_cacheable_paths_are_exactly_meta_and_the_business_routes():
    with client_for(api_db_password=FAKE_PASSWORD) as c:
        # the OpenAPI document lists every route (FastAPI nests included routers)
        routes = {p for p, ops in c.app.openapi()['paths'].items() if 'get' in ops}
    business = {p for p in routes if p.startswith('/api/') and not p.startswith(
        ('/api/health', '/api/docs', '/api/redoc', '/api/openapi'))}
    assert {p.replace('{experiment_id}', 'exp_x') for p in business} == \
        CACHEABLE_PATHS | {'/api/experiments/exp_x'}
    for path in ('/api/health', '/api/health/ready', '/api/docs', '/api/openapi.json'):
        assert not is_cacheable(path)


# ================================================================ unit: no database
@pytest.mark.usefixtures('clean_env')
def test_errors_and_unavailable_database_are_never_cached(monkeypatch):
    with client_for() as c:                         # database unreachable
        async def version(engine):
            return 'run-x'                          # pretend a version is known
        monkeypatch.setattr(c.app.state.data_version, 'get', version)
        for _ in range(2):
            r = c.get('/api/nps')
            assert_problem(r, 503, 'database-unavailable')
            assert 'etag' not in r.headers and 'x-cache' not in r.headers
        assert_problem(c.get('/api/nps?foo=1'), 400, 'invalid-parameter')
        assert len(c.app.state.response_cache) == 0
        for path in ('/api/health', '/api/health/ready'):
            r = c.get(path)
            assert 'etag' not in r.headers and r.headers['cache-control'] == 'no-store'


# ================================================================ integration
@pytest.fixture(scope='module')
def settings():
    s = warehouse_settings()
    owner = config.create_engine(s.postgres_db)
    try:
        with owner.connect() as conn:
            conn.execute(text("SELECT 1 FROM ops.pipeline_runs WHERE step = 'validate_analytics' "
                              "AND status = 'success' LIMIT 1")).one()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built or never validated: {exc}')
    provision(owner, s.api_db_user, s.api_db_password.get_secret_value())
    owner.dispose()
    return s


def client(settings, **overrides):
    return client_for(settings.model_copy(update=overrides))


integration = pytest.mark.integration


@integration
def test_first_get_populates_and_the_repeat_is_served_from_cache(settings):
    with client(settings) as c:
        logs = capture_logs()
        first = c.get('/api/nps')
        assert first.status_code == 200 and first.headers['x-cache'] == 'MISS'
        assert len(c.app.state.response_cache) == 1
        second = c.get('/api/nps')
        assert second.headers['x-cache'] == 'HIT' and second.content == first.content
        assert second.headers['etag'] == first.headers['etag'] and ETAG.match(first.headers['etag'])
        assert second.headers['cache-control'] == 'private, max-age=60'
        assert second.headers['x-request-id'] != first.headers['x-request-id']
        assert second.headers['x-content-type-options'] == 'nosniff'
    lines = [entry for entry in logs() if entry['event'] == 'request']
    assert [line['cache'] for line in lines] == ['miss', 'hit']
    assert lines[0]['db_queries'] > 0 and lines[1]['db_queries'] == 0     # hit: no SQL at all


@integration
def test_parameters_and_endpoints_never_share_entries(settings):
    with client(settings) as c:
        a = c.get('/api/nps', params={'start': '2025-10-01', 'end': '2025-12-31'})
        b = c.get('/api/nps', params={'start': '2025-07-01', 'end': '2025-09-30'})
        assert (a.headers['x-cache'], b.headers['x-cache']) == ('MISS', 'MISS')
        assert a.json()['data'] != b.json()['data'] and a.headers['etag'] != b.headers['etag']
        same = c.get('/api/nps?end=2025-12-31&start=2025-10-01')       # order differs
        assert same.headers['x-cache'] == 'HIT' and same.content == a.content
        s = c.get('/api/support', params={'start': '2025-10-01', 'end': '2025-12-31'})
        assert s.headers['x-cache'] == 'MISS' and s.content != a.content
        e1 = c.get('/api/experiments/exp_onboarding_v2')
        e2 = c.get('/api/experiments/exp_ai_summary_v1')
        assert e1.headers['x-cache'] == e2.headers['x-cache'] == 'MISS'
        assert e1.json()['data']['experiment']['experiment_id'] == 'exp_onboarding_v2'
        assert e2.json()['data']['experiment']['experiment_id'] == 'exp_ai_summary_v1'
        paths = sorted({k[0] for k in c.app.state.response_cache.keys()})
        assert paths == ['/api/experiments/exp_ai_summary_v1', '/api/experiments/exp_onboarding_v2',
                         '/api/nps', '/api/support']
        assert len(c.app.state.response_cache) == 5


@integration
def test_error_responses_are_not_cached(settings):
    with client(settings) as c:
        for path, status, slug in [('/api/nps?foo=1', 400, 'invalid-parameter'),
                                   ('/api/nps?granularity=hour', 422, 'validation-error'),
                                   ('/api/nps?start=2026-01-01', 400, 'invalid-range'),
                                   ('/api/experiments/exp_nope', 404, 'experiment-not-found')]:
            for _ in range(2):
                r = c.get(path)
                assert_problem(r, status, slug)
                assert 'etag' not in r.headers
        assert len(c.app.state.response_cache) == 0


@integration
def test_entries_expire_after_the_ttl(settings):
    with client(settings, api_cache_ttl_s=60) as c:
        clock = Clock()
        c.app.state.response_cache.clock = clock
        assert c.get('/api/revenue').headers['x-cache'] == 'MISS'
        clock.now += 59
        assert c.get('/api/revenue').headers['x-cache'] == 'HIT'
        clock.now += 2
        assert c.get('/api/revenue').headers['x-cache'] == 'MISS'


@integration
def test_the_cache_is_bounded_lru(settings):
    with client(settings, api_cache_max_entries=2) as c:
        for path in ('/api/nps', '/api/support', '/api/revenue'):
            assert c.get(path).headers['x-cache'] == 'MISS'
        cache = c.app.state.response_cache
        assert len(cache) == 2 and cache.evictions == 1
        assert c.get('/api/nps').headers['x-cache'] == 'MISS'       # evicted first
        assert c.get('/api/revenue').headers['x-cache'] == 'HIT'


@integration
def test_entries_are_keyed_by_data_version(settings):
    with client(settings) as c:
        fresh = c.get('/api/nps')
        version = fresh.json()['meta']['data_version']
        assert version and ETAG.match(fresh.headers['etag'])
        cache = c.app.state.response_cache
        # an entry stored under another data version is never served for this one
        cache.clear()
        cache.put(cache_key('/api/nps', '', 'some-older-run'), b'{"stale": true}', 'W/"0"',
                  'application/json')
        r = c.get('/api/nps')
        assert r.headers['x-cache'] == 'MISS' and r.content != b'{"stale": true}'
        assert r.headers['etag'] == fresh.headers['etag'] and r.json()['data'] == fresh.json()['data']
        # the entry for the current version is what a hit returns
        cache.clear()
        cache.put(cache_key('/api/nps', '', version), b'{"current": true}', 'W/"1"',
                  'application/json')
        assert c.get('/api/nps').content == b'{"current": true}'


@integration
def test_every_cacheable_endpoint_gets_an_etag_and_honours_if_none_match(settings):
    paths = sorted(CACHEABLE_PATHS) + ['/api/experiments/exp_onboarding_v2']
    with client(settings) as c:
        for path in paths:
            r = c.get(path)
            assert r.status_code == 200 and ETAG.match(r.headers['etag']), path
            not_modified = c.get(path, headers={'If-None-Match': r.headers['etag']})
            assert not_modified.status_code == 304 and not_modified.content == b'', path
            assert not_modified.headers['etag'] == r.headers['etag']
            assert not_modified.headers['cache-control'] == 'private, max-age=60'
            assert not_modified.headers['x-request-id'] and \
                not_modified.headers['x-frame-options'] == 'DENY'
            changed = c.get(path, headers={'If-None-Match': 'W/"0000"'})
            assert changed.status_code == 200 and changed.content == r.content


@integration
def test_etags_are_per_query_and_agree_across_workers(settings):
    with client(settings) as worker_1:
        a = worker_1.get('/api/support', params={'granularity': 'month'})
        b = worker_1.get('/api/support', params={'granularity': 'week'})
        assert a.headers['etag'] != b.headers['etag']
        r = worker_1.get('/api/support', params={'granularity': 'week'},
                         headers={'If-None-Match': a.headers['etag']})
        assert r.status_code == 200 and r.content == b.content
    with client(settings) as worker_2:                 # separate process cache, cold
        cold = worker_2.get('/api/support', params={'granularity': 'month'},
                            headers={'If-None-Match': a.headers['etag']})
        assert cold.status_code == 304 and cold.headers['x-cache'] == 'MISS'
        regenerated = worker_2.get('/api/support', params={'granularity': 'month'})
        assert regenerated.headers['etag'] == a.headers['etag']


@integration
def test_head_gzip_and_disabled_cache(settings):
    with client(settings) as c:
        full = c.get('/api/feature-adoption')
        head = c.head('/api/feature-adoption')
        assert head.status_code == 200 and head.content == b'' and head.headers['x-cache'] == 'HIT'
        assert head.headers['etag'] == full.headers['etag']
        raw = c.get('/api/feature-adoption', headers={'Accept-Encoding': 'gzip'})
        assert raw.headers['content-encoding'] == 'gzip' and raw.headers['etag'] == full.headers['etag']
        assert json.loads(raw.content if raw.content[:1] == b'{' else gzip.decompress(raw.content)) \
            == full.json()
    with client(settings, api_cache_enabled=False) as c:
        first, second = c.get('/api/nps'), c.get('/api/nps')
        assert first.headers['x-cache'] == second.headers['x-cache'] == 'BYPASS'
        assert len(c.app.state.response_cache) == 0
        assert c.get('/api/nps', headers={'If-None-Match': first.headers['etag']}).status_code == 304


@integration
def test_cached_entries_still_require_the_api_key(settings):
    key = 'k' * 40
    with client(settings, api_auth_mode='api_key', api_keys=[SecretStr(key)]) as c:
        assert c.get('/api/nps', headers={'X-API-Key': key}).headers['x-cache'] == 'MISS'
        assert c.get('/api/nps', headers={'X-API-Key': key}).headers['x-cache'] == 'HIT'
        assert_problem(c.get('/api/nps'), 401, 'unauthorized')
        assert_problem(c.get('/api/nps', headers={'X-API-Key': 'w' * 40}), 401, 'unauthorized')
        denied = c.get('/api/nps', headers={'If-None-Match': '*'})
        assert denied.status_code == 401
        keys_repr = repr(c.app.state.response_cache.keys())
        assert key not in keys_repr


@integration
def test_no_secrets_or_internals_in_cache_keys_or_etags(settings):
    secrets = [v for k, v in os.environ.items()
               if v and len(v) >= 6 and any(m in k.upper() for m in ('PASSWORD', 'SECRET', 'KEY'))]
    with client(settings) as c:
        etags = [c.get(p).headers['etag'] for p in ('/api/overview', '/api/nps', '/api/meta')]
        keys = repr(c.app.state.response_cache.keys())
    for value in secrets + [settings.postgres_host + ':', 'postgresql', settings.api_db_user]:
        assert value not in keys and all(value not in e for e in etags)
    assert all(ETAG.match(e) for e in etags)
