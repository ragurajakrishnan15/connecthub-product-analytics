// Tests for the dashboard data layer (PHASE_5_PLAN.md section 3). The code under test is
// extracted from index.html between the DATA LAYER markers, so these tests run what ships.
// Run through pytest (tests/test_dashboard_data_layer.py) or: node --test tests/dashboard
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const html = readFileSync(resolve(root, 'index.html'), 'utf8');
const BEGIN = '// ===== DATA LAYER: BEGIN =====';
const END = '// ===== DATA LAYER: END =====';
const source = html.slice(html.indexOf(BEGIN), html.indexOf(END));
assert.ok(source.length > 1000, 'data layer markers not found in index.html');

const L = new Function(`${source}
  return { ApiError, ENDPOINTS, createApiClient, describeError, createPanelLoader,
           promptForApiKey, buildUrl, KEY_STORAGE };`)();
const { ApiError, ENDPOINTS, createApiClient, describeError, createPanelLoader, promptForApiKey, buildUrl } = L;
const openapi = JSON.parse(readFileSync(resolve(root, 'docs', 'openapi.json'), 'utf8'));

const ORIGIN = 'http://127.0.0.1:8000';
const KEY = 'test-key-0123456789abcdef';

// ---------------------------------------------------------------- helpers
const json = (data, { status = 200, etag = 'W/"v1"', meta = {}, headers = {} } = {}) =>
  new Response(JSON.stringify({ data, meta }), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-1', ...(etag ? { ETag: etag } : {}), ...headers },
  });
const notModified = () => new Response(null, { status: 304, headers: { 'X-Request-ID': 'req-304' } });
const problem = (slug, status, extra = {}, headers = {}) =>
  new Response(JSON.stringify({ type: `urn:connecthub:problem:${slug}`, title: 'T', status, detail: 'detail text',
                                instance: '/api/x', request_id: 'rid-9', ...extra }),
               { status, headers: { 'Content-Type': 'application/problem+json', ...headers } });
const hang = signal => new Promise((_, reject) =>
  signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function storage(initial = {}) {
  const data = { ...initial };
  return { data, getItem: k => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, removeItem: k => { delete data[k]; } };
}
function setup(handler, options = {}) {
  const calls = [];
  const fetch = async (url, init) => { calls.push({ url, init }); return handler({ url, init, n: calls.length, calls }); };
  const api = createApiClient({ baseUrl: ORIGIN, fetch, storage: storage(), ...options });
  return { api, calls };
}
const headerOf = (call, name) => call.init.headers[name];

// ---------------------------------------------------------------- contract with the API
describe('the endpoint table matches the committed OpenAPI contract', () => {
  const paths = openapi.paths;
  const fromSpec = name => {
    const spec = ENDPOINTS[name];
    return spec.pathParam ? '/api/experiments/{experiment_id}' : '/api' + spec.path;
  };

  it('covers every data endpoint (everything except the two health routes)', () => {
    const covered = new Set(Object.keys(ENDPOINTS).map(fromSpec));
    const expected = Object.keys(paths).filter(p => !p.startsWith('/api/health'));
    assert.deepEqual([...covered].sort(), expected.sort());
  });

  for (const name of Object.keys(ENDPOINTS)) {
    it(`${name}: same path and the same query parameters`, () => {
      const op = paths[fromSpec(name)].get;
      assert.ok(op, 'GET operation exists');
      const declared = (op.parameters || []).filter(p => p.in === 'query').map(p => p.name).sort();
      assert.deepEqual([...ENDPOINTS[name].params].sort(), declared);
    });
  }

  it('every endpoint is read-only (GET only)', () => {
    for (const name of Object.keys(ENDPOINTS)) assert.deepEqual(Object.keys(paths[fromSpec(name)]), ['get']);
  });
});

// ---------------------------------------------------------------- building requests
describe('request URLs', () => {
  it('sorts parameters, drops empty ones and repeats arrays', () => {
    const url = buildUrl(ORIGIN, 'featureAdoption', { max_day: 30, features: ['sms', 'ai_assist'], start_month: '', end_month: null });
    assert.equal(url, `${ORIGIN}/api/feature-adoption?features=sms&features=ai_assist&max_day=30`);
    assert.equal(buildUrl(ORIGIN, 'overview'), `${ORIGIN}/api/overview`);
    assert.equal(buildUrl(ORIGIN, 'activation', { include_incomplete: false, granularity: 'week' }),
                 `${ORIGIN}/api/activation?granularity=week&include_incomplete=false`);
  });

  it('is stable regardless of parameter order', () => {
    assert.equal(buildUrl(ORIGIN, 'nps', { start: '2025-01-01', end: '2025-02-01' }),
                 buildUrl(ORIGIN, 'nps', { end: '2025-02-01', start: '2025-01-01' }));
  });

  it('refuses undeclared parameters, unknown endpoints and non-scalar values', () => {
    assert.throws(() => buildUrl(ORIGIN, 'overview', { x: 1 }), TypeError);
    assert.throws(() => buildUrl(ORIGIN, 'nope'), TypeError);
    assert.throws(() => buildUrl(ORIGIN, 'nps', { start: { a: 1 } }), TypeError);
  });

  it('encodes and validates the experiment id', () => {
    assert.equal(buildUrl(ORIGIN, 'experiment', {}, 'exp_onboarding_v2'), `${ORIGIN}/api/experiments/exp_onboarding_v2`);
    for (const bad of ['', '../x', 'a/b', 'a b', 'x'.repeat(65), undefined, 'a?b=1'])
      assert.throws(() => buildUrl(ORIGIN, 'experiment', {}, bad), TypeError, String(bad));
  });

  it('values are URL-encoded, so a parameter can never add another', () => {
    const url = buildUrl(ORIGIN, 'nps', { plan_tier: 'a&b=c#d' });
    assert.equal(new URL(url).searchParams.get('plan_tier'), 'a&b=c#d');
    assert.equal([...new URL(url).searchParams.keys()].length, 1);
  });
});

describe('same-origin only', () => {
  it('refuses a base that is not an http(s) origin without credentials', () => {
    for (const bad of ['null', 'file:///c:/x/index.html', 'ftp://h/', 'http://user:pw@h/', '', undefined]) {
      assert.throws(() => createApiClient({ baseUrl: bad, fetch: async () => json({}) }),
                    e => e instanceof ApiError && e.kind === 'config', String(bad));
    }
  });

  it('every request goes to the base origin, as a credential-less GET', async () => {
    const { api, calls } = setup(() => json({ kpis: { a: 1 } }));
    await api.overview();
    const { url, init } = calls[0];
    assert.ok(url.startsWith(ORIGIN + '/api/'));
    assert.equal(init.method, 'GET');
    assert.equal(init.credentials, 'omit');
    assert.equal(init.redirect, 'error');
    assert.equal(init.body, undefined);
    assert.equal(headerOf(calls[0], 'Accept'), 'application/json');
  });
});

// ---------------------------------------------------------------- ETag / 304
describe('ETag revalidation', () => {
  const payload = { kpis: { mrr_usd: { value: 1 } } };

  it('first request is unconditional; the repeat sends If-None-Match and 304 reuses the cached response', async () => {
    const { api, calls } = setup(({ n }) => (n === 1 ? json(payload, { etag: 'W/"abc"' }) : notModified()));
    const first = await api.overview();
    assert.equal(headerOf(calls[0], 'If-None-Match'), undefined);
    assert.equal(first.status, 200);
    assert.equal(first.source, 'network');
    assert.equal(first.etag, 'W/"abc"');

    const second = await api.overview();
    assert.equal(headerOf(calls[1], 'If-None-Match'), 'W/"abc"');
    assert.equal(second.status, 304);
    assert.equal(second.source, 'revalidated');
    assert.deepEqual(second.data, first.data);
    assert.equal(second.empty, false);
  });

  it('returns a private copy: changing a result cannot corrupt the cache', async () => {
    const { api } = setup(({ n }) => (n === 1 ? json(payload) : notModified()));
    const first = await api.overview();
    first.data.kpis.mrr_usd.value = 999;
    const second = await api.overview();
    assert.equal(second.data.kpis.mrr_usd.value, 1);
    second.data.kpis.mrr_usd.value = 5;
    assert.equal((await api.overview()).data.kpis.mrr_usd.value, 1);
  });

  it('a new 200 replaces the stored ETag and body', async () => {
    const { api, calls } = setup(({ n }) => {
      if (n === 1) return json({ kpis: { v: 1 } }, { etag: 'W/"one"' });
      if (n === 2) return json({ kpis: { v: 2 } }, { etag: 'W/"two"' });
      return notModified();
    });
    await api.overview();
    const changed = await api.overview();
    assert.equal(headerOf(calls[1], 'If-None-Match'), 'W/"one"');
    assert.equal(changed.status, 200);
    assert.equal(changed.data.kpis.v, 2);
    const again = await api.overview();
    assert.equal(headerOf(calls[2], 'If-None-Match'), 'W/"two"');
    assert.equal(again.data.kpis.v, 2);
  });

  it('keeps one entry per URL, so different parameters never share validators', async () => {
    const { api, calls } = setup(({ url }) => json({ points: [{}] }, { etag: `W/"${new URL(url).searchParams.get('granularity')}"` }));
    await api.engagement({ granularity: 'week' });
    await api.engagement({ granularity: 'month' });
    await api.engagement({ granularity: 'week' });
    assert.equal(headerOf(calls[1], 'If-None-Match'), undefined);
    assert.equal(headerOf(calls[2], 'If-None-Match'), 'W/"week"');
  });

  it('does not cache a response without an ETag', async () => {
    const { api, calls } = setup(() => json(payload, { etag: null }));
    await api.overview();
    await api.overview();
    assert.equal(headerOf(calls[1], 'If-None-Match'), undefined);
  });

  it('a 304 with nothing stored is retried once without validators', async () => {
    const { api, calls } = setup(({ n }) => (n === 1 ? notModified() : json(payload)));
    const r = await api.overview();
    assert.equal(calls.length, 2);
    assert.equal(r.source, 'network');
    assert.equal(headerOf(calls[1], 'If-None-Match'), undefined);
  });

  it('a second unexpected 304 is a protocol error, not a loop', async () => {
    const { api, calls } = setup(() => notModified());
    await assert.rejects(api.overview(), e => e.kind === 'protocol');
    assert.equal(calls.length, 2);
  });

  it('the cache is bounded (least recently used out)', async () => {
    const { api, calls } = setup(({ url }) => json({ points: [{}] }, { etag: `W/"${url}"` }), { maxCached: 2 });
    await api.engagement({ granularity: 'day' });
    await api.engagement({ granularity: 'week' });
    await api.engagement({ granularity: 'month' });          // evicts "day"
    await api.engagement({ granularity: 'day' });
    assert.equal(headerOf(calls[3], 'If-None-Match'), undefined);
    await api.engagement({ granularity: 'month' });
    assert.ok(headerOf(calls[4], 'If-None-Match'));
  });

  it('identical concurrent requests share one fetch; a request with its own signal does not', async () => {
    const gate = deferred();
    const { api, calls } = setup(() => gate.promise.then(() => json(payload)));   // a fresh Response per fetch
    const a = api.overview();
    const b = api.overview();
    const c = api.overview({}, { signal: new AbortController().signal });
    gate.resolve();
    await Promise.all([a, b, c]);
    assert.equal(calls.length, 2);
  });
});

// ---------------------------------------------------------------- errors
describe('HTTP errors', () => {
  const failing = (response) => setup(() => response());

  it('503 data-not-ready is a retryable "unavailable" with the request id and Retry-After', async () => {
    const { api } = failing(() => problem('data-not-ready', 503, {}, { 'Retry-After': '5' }));
    await assert.rejects(api.overview(), e => {
      assert.equal(e.kind, 'unavailable');
      assert.equal(e.status, 503);
      assert.equal(e.type, 'data-not-ready');
      assert.equal(e.requestId, 'rid-9');
      assert.equal(e.retryAfterS, 5);
      assert.equal(e.retryable, true);
      return true;
    });
  });

  it('maps the other statuses to kinds', async () => {
    const cases = [
      [() => problem('query-timeout', 504), 'server-timeout', 'query-timeout'],
      [() => problem('invalid-range', 400), 'validation', 'invalid-range'],
      [() => problem('validation-error', 422, { errors: [{ field: 'start' }] }), 'validation', 'validation-error'],
      [() => problem('not-found', 404), 'not-found', 'not-found'],
      [() => problem('internal-error', 500), 'server', 'internal-error'],
      [() => problem('unauthorized', 401), 'auth', 'unauthorized'],
    ];
    for (const [make, kind, slug] of cases) {
      const { api } = failing(make);
      await assert.rejects(api.overview(), e => e instanceof ApiError && e.kind === kind && e.type === slug, slug);
    }
  });

  it('keeps field errors of a 422', async () => {
    const { api } = failing(() => problem('validation-error', 422, { errors: [{ field: 'start', message: 'bad' }] }));
    await assert.rejects(api.overview(), e => e.errors[0].field === 'start');
  });

  it('never echoes a non-JSON error body', async () => {
    const { api } = failing(() => new Response('<html><script>alert(1)</script>', { status: 502, headers: { 'Content-Type': 'text/html' } }));
    await assert.rejects(api.overview(), e => {
      assert.equal(e.kind, 'server');
      assert.equal(e.detail, null);
      assert.ok(!/script|html/i.test(e.message));
      return true;
    });
  });

  it('a transport failure is a retryable "network" error', async () => {
    const { api } = setup(() => { throw new TypeError('Failed to fetch'); });
    await assert.rejects(api.overview(), e => e.kind === 'network' && e.retryable === true);
  });

  it('rejects responses that are not valid API envelopes', async () => {
    const cases = [
      new Response('not json', { status: 200, headers: { 'Content-Type': 'application/json' } }),
      new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
      new Response('{"data":[],"meta":{}}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
      new Response('{"data":{}}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
      new Response('{"data":{},"meta":{}}', { status: 200, headers: { 'Content-Type': 'text/html' } }),
    ];
    for (const res of cases) {
      const { api } = setup(() => res);
      await assert.rejects(api.overview(), e => e.kind === 'parse', await res.clone().text());
    }
  });

  it('a failed response is never cached', async () => {
    const { api, calls } = setup(({ n }) => (n === 1 ? problem('database-unavailable', 503) : json({ kpis: { a: 1 } })));
    await assert.rejects(api.overview());
    const r = await api.overview();
    assert.equal(r.status, 200);
    assert.equal(headerOf(calls[1], 'If-None-Match'), undefined);
  });
});

describe('timeout and cancellation', () => {
  it('a hung request times out as "timeout"', async () => {
    const { api } = setup(({ init }) => hang(init.signal), { timeoutMs: 25 });
    await assert.rejects(api.overview(), e => e.kind === 'timeout' && e.retryable === true);
  });

  it('a caller abort is "aborted" and not retryable', async () => {
    const ctl = new AbortController();
    const { api } = setup(({ init }) => hang(init.signal), { timeoutMs: 5000 });
    const p = api.overview({}, { signal: ctl.signal });
    setTimeout(() => ctl.abort(), 10);
    await assert.rejects(p, e => e.kind === 'aborted' && e.retryable === false);
  });

  it('an already-aborted signal makes no request at all', async () => {
    const ctl = new AbortController();
    ctl.abort();
    const { api, calls } = setup(({ init }) => hang(init.signal));
    await assert.rejects(api.overview({}, { signal: ctl.signal }), e => e.kind === 'aborted');
    assert.equal(calls.length, 0);
  });

  it('a stalled body counts against the timeout too', async () => {
    const stalled = ({ init }) => ({
      status: 200, ok: true,
      headers: new Headers({ 'Content-Type': 'application/json' }),
      text: () => hang(init.signal),
    });
    const { api } = setup(stalled, { timeoutMs: 25 });
    await assert.rejects(api.overview(), e => e.kind === 'timeout');
  });
});

// ---------------------------------------------------------------- empty data
describe('empty-data detection', () => {
  const full = {
    meta: {}, overview: { kpis: { a: 1 } }, engagement: { points: [{}] }, activation: { signups: 5, funnel: [{}] },
    retention: { pooled_curve: [{}] }, cohorts: { cohorts: [{}] }, revenue: { series: [{}] },
    featureAdoption: { curves: [{}] }, experiments: { experiments: [{}] }, experiment: { experiment: { experiment_id: 'x' } },
    nps: { summary: { responses: 40 } }, support: { ai_agent: { calls: 3 }, tickets: { created: 0 } },
    customerHealth: { workspaces: 10 }, workspaces: { items: [{}] },
  };
  const empty = {
    meta: null, overview: { kpis: {} }, engagement: { points: [] }, activation: { signups: 0, funnel: [{}] },
    retention: { pooled_curve: [] }, cohorts: { cohorts: [] }, revenue: { series: [] },
    featureAdoption: { curves: [] }, experiments: { experiments: [] }, experiment: {},
    nps: { summary: { responses: 0 } }, support: { ai_agent: { calls: 0 }, tickets: { created: 0 } },
    customerHealth: { workspaces: 0 }, workspaces: { items: [] },
  };

  it('has a case for every endpoint', () => {
    assert.deepEqual(Object.keys(full).sort(), Object.keys(ENDPOINTS).sort());
  });

  for (const name of Object.keys(full)) {
    it(`${name}: non-empty data is not empty`, async () => {
      const { api } = setup(() => json(full[name]));
      const r = await (name === 'experiment' ? api.experiment('x') : api[name]());
      assert.equal(r.empty, false);
    });
    if (empty[name] !== null) {
      it(`${name}: a successful but empty response is flagged empty`, async () => {
        const { api } = setup(() => json(empty[name]));
        const r = await (name === 'experiment' ? api.experiment('x') : api[name]());
        assert.equal(r.empty, true);
      });
    }
  }
});

// ---------------------------------------------------------------- API key
describe('API key handling', () => {
  const ok = () => json({ kpis: { a: 1 } });

  it('sends no key header until one is set; then sends it only as X-API-Key', async () => {
    const { api, calls } = setup(ok);
    await api.overview();
    assert.equal(headerOf(calls[0], 'X-API-Key'), undefined);
    assert.equal(api.hasApiKey(), false);
    assert.equal(api.setApiKey(KEY), true);
    await api.engagement();
    assert.equal(headerOf(calls[1], 'X-API-Key'), KEY);
    assert.ok(!calls[1].url.includes(KEY), 'key must never be in the URL');
    assert.equal(api.hasApiKey(), true);
  });

  it('keeps the key in sessionStorage only (and trims it)', () => {
    const store = storage();
    const { api } = setup(ok, { storage: store });
    api.setApiKey('  ' + KEY + '  ');
    assert.deepEqual(store.data, { [L.KEY_STORAGE]: KEY });
    api.clearApiKey();
    assert.deepEqual(store.data, {});
  });

  it('a key stored by an earlier page view is used', async () => {
    const { api, calls } = setup(ok, { storage: storage({ [L.KEY_STORAGE]: KEY }) });
    await api.overview();
    assert.equal(headerOf(calls[0], 'X-API-Key'), KEY);
  });

  it('rejects keys that cannot be a header value', () => {
    const { api } = setup(ok);
    for (const bad of ['', '   ', 'has space', 'new\nline', 'tab\tkey', 'ünïcode', 'x'.repeat(257), null, undefined, 42])
      assert.equal(api.setApiKey(bad), false, JSON.stringify(bad));
    assert.equal(api.hasApiKey(), false);
  });

  it('falls back to memory when storage throws', async () => {
    const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); } };
    const { api, calls } = setup(ok, { storage: broken });
    assert.equal(api.setApiKey(KEY), true);
    await api.overview();
    assert.equal(headerOf(calls[0], 'X-API-Key'), KEY);
    api.clearApiKey();
    assert.equal(api.hasApiKey(), false);
  });

  it('a 401 without a prompt handler is an auth error', async () => {
    const { api } = setup(() => problem('unauthorized', 401));
    await assert.rejects(api.overview(), e => e.kind === 'auth' && e.keyRejected !== true);
  });

  it('a rejected key is forgotten, not resent', async () => {
    const store = storage();
    const { api, calls } = setup(({ n }) => (n === 1 ? problem('unauthorized', 401) : problem('unauthorized', 401)), { storage: store });
    api.setApiKey(KEY);
    await assert.rejects(api.overview(), e => e.keyRejected === true);
    assert.deepEqual(store.data, {});
    await assert.rejects(api.overview());
    assert.equal(headerOf(calls[1], 'X-API-Key'), undefined);
  });

  it('prompts once, then retries once with the entered key', async () => {
    const prompts = [];
    const api0 = { current: null };
    const { api, calls } = setup(({ init }) => (headerOf({ init }, 'X-API-Key') === KEY ? ok() : problem('unauthorized', 401)), {
      onAuthRequired: async info => { prompts.push(info); return api0.current.setApiKey(KEY); },
    });
    api0.current = api;
    const r = await api.overview();
    assert.equal(r.status, 200);
    assert.deepEqual(prompts.map(p => p.reason), ['missing']);
    assert.equal(calls.length, 2);
    assert.equal(headerOf(calls[1], 'X-API-Key'), KEY);
  });

  it('a key that is rejected again does not loop and does not prompt again', async () => {
    let prompts = 0;
    const holder = {};
    const { api, calls } = setup(() => problem('unauthorized', 401), {
      onAuthRequired: async () => { prompts += 1; return holder.api.setApiKey(KEY); },
    });
    holder.api = api;
    await assert.rejects(api.overview(), e => e.kind === 'auth');
    assert.equal(prompts, 1);
    assert.equal(calls.length, 2);
  });

  it('reports "invalid" when a stored key was rejected', async () => {
    const reasons = [];
    const { api } = setup(() => problem('unauthorized', 401), {
      storage: storage({ [L.KEY_STORAGE]: KEY }),
      onAuthRequired: async info => { reasons.push(info.reason); return false; },
    });
    await assert.rejects(api.overview());
    assert.deepEqual(reasons, ['invalid']);
  });

  it('dismissing the prompt leaves the auth error', async () => {
    const { api, calls } = setup(() => problem('unauthorized', 401), { onAuthRequired: async () => false });
    await assert.rejects(api.overview(), e => e.kind === 'auth');
    assert.equal(calls.length, 1);
  });

  it('a prompt handler that throws is treated as dismissed', async () => {
    const { api } = setup(() => problem('unauthorized', 401), { onAuthRequired: async () => { throw new Error('boom'); } });
    await assert.rejects(api.overview(), e => e.kind === 'auth');
  });

  it('simultaneous 401s open one prompt and every request then succeeds', async () => {
    let prompts = 0;
    const holder = {};
    const { api } = setup(({ init }) => (headerOf({ init }, 'X-API-Key') === KEY ? ok() : problem('unauthorized', 401)), {
      onAuthRequired: async () => { prompts += 1; await new Promise(r => setTimeout(r, 15)); return holder.api.setApiKey(KEY); },
    });
    holder.api = api;
    const results = await Promise.all([api.overview(), api.engagement(), api.nps()]);
    assert.equal(prompts, 1);
    assert.ok(results.every(r => r.status === 200));
  });
});

// ---------------------------------------------------------------- messages
describe('describeError', () => {
  const make = (kind, extra) => new ApiError(kind, 'msg', extra);

  it('explains a warehouse that has not been built', () => {
    const d = describeError(make('unavailable', { type: 'data-not-ready', requestId: 'r1' }));
    assert.equal(d.title, 'Warehouse not ready');
    assert.match(d.message, /pipeline/);
    assert.equal(d.requestId, 'r1');
    assert.equal(d.retryable, true);
  });

  it('covers every transport and status kind with plain-text copy', () => {
    for (const kind of ['network', 'timeout', 'auth', 'validation', 'not-found', 'unavailable', 'server-timeout', 'server', 'parse', 'protocol', 'config']) {
      const d = describeError(make(kind, {}));
      assert.ok(d.title && d.message, kind);
    }
  });

  it('uses the API detail for a rejected request, as plain text', () => {
    const d = describeError(make('validation', { type: 'invalid-range', detail: 'start must be <= end' }));
    assert.equal(d.message, 'start must be <= end');
  });

  it('is not retryable for an auth error or a cancelled request', () => {
    assert.equal(describeError(make('auth', {})).retryable, false);
    assert.equal(describeError(make('aborted', {})).retryable, false);
  });
});

// ---------------------------------------------------------------- loading state
describe('panel loader', () => {
  it('reports loading, then ready', async () => {
    const states = [];
    const loader = createPanelLoader(s => states.push(s.state));
    const { api } = setup(() => json({ kpis: { a: 1 } }));
    const result = await loader.run(signal => api.overview({}, { signal }));
    assert.deepEqual(states, ['loading', 'ready']);
    assert.equal(result.status, 200);
  });

  it('reports empty for a successful response with nothing to draw', async () => {
    const states = [];
    const loader = createPanelLoader(s => states.push(s.state));
    const { api } = setup(() => json({ points: [] }));
    await loader.run(signal => api.engagement({}, { signal }));
    assert.deepEqual(states, ['loading', 'empty']);
  });

  it('reports an error with display copy', async () => {
    const seen = [];
    const loader = createPanelLoader(s => seen.push(s));
    const { api } = setup(() => problem('data-not-ready', 503));
    assert.equal(await loader.run(signal => api.overview({}, { signal })), null);
    assert.equal(seen[1].state, 'error');
    assert.equal(seen[1].message.title, 'Warehouse not ready');
    assert.equal(seen[1].error.kind, 'unavailable');
  });

  it('drops a stale response: the newer run wins and the older request is aborted', async () => {
    const states = [];
    const loader = createPanelLoader(s => states.push([s.state, s.result && s.result.data.tag]));
    const slow = deferred();
    const signals = [];
    const first = loader.run(signal => { signals.push(signal); return slow.promise; });
    const second = loader.run(async signal => { signals.push(signal); return { empty: false, data: { tag: 'second' } }; });
    await second;
    slow.resolve({ empty: false, data: { tag: 'first' } });          // arrives late
    assert.equal(await first, null);
    assert.equal(signals[0].aborted, true);
    assert.deepEqual(states, [['loading', undefined], ['loading', undefined], ['ready', 'second']]);
  });

  it('a late error from a superseded run is not shown', async () => {
    const states = [];
    const loader = createPanelLoader(s => states.push(s.state));
    const slow = deferred();
    const first = loader.run(() => slow.promise);
    await loader.run(async () => ({ empty: false, data: {} }));
    slow.reject(new ApiError('network', 'down'));
    await first;
    assert.deepEqual(states, ['loading', 'loading', 'ready']);
  });

  it('cancel() abandons the run without any further state', async () => {
    const states = [];
    const loader = createPanelLoader(s => states.push(s.state));
    const { api } = setup(({ init }) => hang(init.signal), { timeoutMs: 5000 });
    const run = loader.run(signal => api.overview({}, { signal }));
    loader.cancel();
    assert.equal(await run, null);
    assert.deepEqual(states, ['loading']);
  });
});

// ---------------------------------------------------------------- the key dialog
describe('API key dialog', () => {
  class El {
    constructor(tag) { this.tag = tag; this.children = []; this.handlers = {}; this.attrs = {}; this.className = ''; this._text = ''; this.open = false; this.parent = null; this.value = ''; }
    set innerHTML(_) { throw new Error('innerHTML must not be used'); }
    set textContent(v) { this._text = String(v); }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    appendChild(c) { this.children.push(c); c.parent = this; return c; }
    append(...cs) { cs.forEach(c => this.appendChild(c)); }
    setAttribute(k, v) { this.attrs[k] = v; }
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
    fire(type) { const ev = { prevented: false, preventDefault() { this.prevented = true; } }; (this.handlers[type] || []).forEach(f => f(ev)); return ev; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
    focus() { this.focused = true; }
    find(pred) { return pred(this) ? this : this.children.map(c => c.find(pred)).find(Boolean) || null; }
  }
  const fakeDoc = () => ({ createElement: tag => new El(tag), body: new El('body') });
  const parts = doc => {
    const dialog = doc.body.children[0];
    return { dialog, form: dialog.find(e => e.tag === 'form'), input: dialog.find(e => e.tag === 'input'),
             cancel: dialog.find(e => e.tag === 'button' && e.type === 'button'),
             error: dialog.find(e => e.className === 'key-dialog-error'),
             text: dialog.find(e => e.tag === 'p' && !e.className) };
  };

  it('opens a modal password field and resolves with the trimmed key on submit', async () => {
    const doc = fakeDoc();
    const result = promptForApiKey(doc, { reason: 'missing' });
    const { dialog, form, input } = parts(doc);
    assert.equal(dialog.open, true);
    assert.equal(dialog.className, 'key-dialog');
    assert.equal(input.type, 'password');
    assert.equal(input.autocomplete, 'off');
    input.value = `  ${KEY} `;
    assert.equal(form.fire('submit').prevented, true);
    assert.equal(await result, KEY);
    assert.equal(doc.body.children.length, 0, 'dialog removed');
  });

  it('refuses an invalid key without closing', async () => {
    const doc = fakeDoc();
    let settled = false;
    const result = promptForApiKey(doc).then(v => { settled = true; return v; });
    const { dialog, form, input, error } = parts(doc);
    input.value = 'has space';
    form.fire('submit');
    await new Promise(r => setTimeout(r, 5));
    assert.equal(settled, false);
    assert.equal(dialog.open, true);
    assert.match(error.textContent, /no spaces/);
    input.value = KEY;
    form.fire('submit');
    assert.equal(await result, KEY);
  });

  it('Cancel and Escape resolve null', async () => {
    let doc = fakeDoc();
    let result = promptForApiKey(doc);
    parts(doc).cancel.fire('click');
    assert.equal(await result, null);
    doc = fakeDoc();
    result = promptForApiKey(doc);
    const escape = parts(doc).dialog.fire('cancel');
    assert.equal(escape.prevented, true);
    assert.equal(await result, null);
  });

  it('words a rejected key differently and uses text nodes only', async () => {
    const doc = fakeDoc();
    const result = promptForApiKey(doc, { reason: 'invalid' });
    assert.match(parts(doc).text.textContent, /rejected/);
    parts(doc).cancel.fire('click');
    await result;
  });
});

// ---------------------------------------------------------------- source hygiene
describe('the data layer source', () => {
  it('uses no innerHTML, eval, random values, XHR or persistent storage', () => {
    for (const banned of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/, /new Function/,
                          /Math\.random/, /XMLHttpRequest/, /localStorage/, /\bIndexedDB\b/i, /\bcookie\b/i]) {
      assert.ok(!banned.test(source), String(banned));
    }
  });

  it('touches no browser globals directly: window, document and location are injected', () => {
    assert.ok(!/\b(window|document|location|navigator)\b/.test(source.replace(/\/\/.*$/gm, '')));
  });

  it('calls the network only through the injected fetch, and only the /api prefix', () => {
    const code = source.replace(/\/\/.*$/gm, '');
    assert.ok(!/(^|[^.\w])fetch\(/.test(code));
    assert.equal((code.match(/o\.fetch\(/g) || []).length, 1);
    assert.equal((code.match(/https?:\/\/[^\s'"]*/g) || []).filter(u => !u.includes('127.0.0.1')).length, 0);
  });

  it('holds no analytics numbers: only configuration constants', () => {
    // The only numeric literals: timeout, cache and key limits, ms-to-s, HTTP statuses, indexes.
    // (String patterns are matched within one line so they cannot swallow real code.)
    const code = source.replace(/\/\/.*$/gm, '').replace(/'[^'\n]*'|"[^"\n]*"|`[^`\n]*`/g, "''");
    const numbers = [...code.matchAll(/(?<![\w.$])\d+(?:\.\d+)?(?![\w.])/g)].map(m => Number(m[0]));
    assert.ok(numbers.includes(401) && numbers.includes(503), 'the scan really sees the code');
    const allowed = new Set([0, 1, 64, 256, 1000, 10000, 304, 400, 401, 404, 422, 500, 503, 504]);
    assert.deepEqual([...new Set(numbers.filter(n => !allowed.has(n)))], []);
  });
});

// ---------------------------------------------------------------- how the page uses it
describe('page wiring', () => {
  const script = html.slice(html.indexOf(END));
  it('creates the client for the page origin only, with no alternative API host', () => {
    assert.match(script, /baseUrl:\s*location\.origin/);
    assert.ok(!/URLSearchParams\(location/.test(html));
    assert.ok(!/CONNECTHUB_API_BASE/.test(html));
  });

  it('sends nothing at load: no request is made until a panel asks', () => {
    const code = html.slice(html.indexOf(END)).replace(/\/\/.*$/gm, '');
    assert.ok(!/\bapi\.[a-zA-Z]+\(/.test(code.replace(/api\.setApiKey\(key\)/, '')));
  });
});
