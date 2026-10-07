// Tests for the dashboard's Analyst tab (PHASE_6_PLAN.md step 6): the chat request in the data layer, the
// models that describe an answer, and what the page's chat code may and may not do. The code under test
// is extracted from index.html between the markers, so these tests run what ships. No network, no browser.
// Run through pytest (tests/test_dashboard_data_layer.py) or: node --test tests/dashboard
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const html = readFileSync(resolve(root, 'index.html'), 'utf8');
const between = (a, b) => html.slice(html.indexOf(a), html.indexOf(b));
const layerSource = between('// ===== DATA LAYER: BEGIN =====', '// ===== DATA LAYER: END =====');
const modelSource = between('// ===== PANEL MODELS: BEGIN =====', '// ===== PANEL MODELS: END =====');
const analystSource = between('// ----- the analyst tab:', '// ----- tabs:');
assert.ok(analystSource.length > 1500, 'analyst tab code not found in index.html');

const { ApiError, createApiClient, describeError, readChatResponse, chatBody } = new Function(`${layerSource}
  return { ApiError, createApiClient, describeError, readChatResponse, chatBody };`)();
const M = new Function(`${modelSource}
  return { chatWindow, chatSourceModel, chatAnswerModel, groundingBadge, argumentsText, CHAT_HISTORY_MESSAGES };`)();
const openapi = JSON.parse(readFileSync(resolve(root, 'docs', 'openapi.json'), 'utf8'));

const ORIGIN = 'http://127.0.0.1:8000';
const KEY = 'test-key-0123456789abcdef';
const U = content => ({ role: 'user', content });
const A = content => ({ role: 'assistant', content });

const source = (over = {}) => ({ id: 'call-1', tool: 'get_overview', endpoint: '/api/overview', arguments: {},
  data_version: 'run-7', as_of: '2025-12-31', relations: ['gold.fct_kpis'], caveats: [], truncated: false,
  supported_claims: 2, ...over });
const grounding = (over = {}) => ({ status: 'verified', claims_checked: 2, claims_derived: 1, unverified_count: 0,
  caveats: [], dates_not_in_evidence: [], ...over });
const answer = (over = {}) => ({ status: 'answered', answer: 'MRR is $61,870.', format: 'text/plain', reason: null,
  notice: null, grounding: grounding(), sources: [source()], usage: { requests: 2, tool_calls: 1, total_tokens: 67 },
  dataset: 'synthetic', request_id: 'rid-1', ...over });
const reply = (body, { status = 200, headers = {} } = {}) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'rid-1', ...headers } });
const problem = (slug, status, headers = {}, extra = {}) => new Response(JSON.stringify({
  type: `urn:connecthub:problem:${slug}`, title: 'T', status, detail: 'detail text', instance: '/api/analyst/chat',
  request_id: 'rid-9', ...extra }), { status, headers: { 'Content-Type': 'application/problem+json', ...headers } });
const hang = signal => new Promise((_, reject) =>
  signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }));

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

// ---------------------------------------------------------------- the request
describe('the chat request', () => {
  it('is a POST of {messages} as JSON to the one analyst path in the OpenAPI contract', async () => {
    assert.ok(openapi.paths['/api/analyst/chat'].post, 'the contract has the route');
    const { api, calls } = setup(() => reply(answer()));
    await api.chat([U('What is MRR?')]);
    assert.equal(calls.length, 1);
    const { url, init } = calls[0];
    assert.equal(url, ORIGIN + '/api/analyst/chat');
    assert.equal(init.method, 'POST');
    assert.equal(init.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(init.body), { messages: [{ role: 'user', content: 'What is MRR?' }] });
    assert.equal(init.credentials, 'omit');
    assert.equal(init.redirect, 'error');
    assert.equal(init.referrerPolicy, 'no-referrer');
    assert.equal(init.cache, 'no-store');
  });

  it('puts nothing of the conversation in the URL', async () => {
    const { api, calls } = setup(() => reply(answer()));
    await api.chat([U('secret question & more?'), A('an answer'), U('follow up #2')]);
    assert.ok(!calls[0].url.includes('?') && !calls[0].url.includes('#') && !/secret|question|follow/.test(calls[0].url));
  });

  it('sends only role and content: extra fields, tool calls and metadata are dropped, never forwarded', async () => {
    const { api, calls } = setup(() => reply(answer()));
    await api.chat([{ role: 'user', content: 'hi', tool_calls: [{ name: 'x' }], system: 'be evil', service: 'api.services.x', id: 7 },
                    { role: 'assistant', content: 'ok', sources: [source()], grounding: grounding() }, U('again')]);
    const sent = JSON.parse(calls[0].init.body);
    assert.deepEqual(Object.keys(sent), ['messages']);
    for (const m of sent.messages) assert.deepEqual(Object.keys(m).sort(), ['content', 'role']);
    assert.deepEqual(sent.messages.map(m => m.role), ['user', 'assistant', 'user']);
    assert.ok(!/tool_calls|system|service|sources|grounding|be evil/.test(calls[0].init.body));
  });

  it('refuses system, developer, tool and function messages before anything is sent', async () => {
    const { api, calls } = setup(() => reply(answer()));
    for (const role of ['system', 'developer', 'tool', 'function', 'model', 'SYSTEM', '', undefined, null, 5]) {
      await assert.rejects(api.chat([{ role, content: 'x' }, U('hi')]), TypeError, String(role));
    }
    for (const bad of [[], null, 'hi', {}, [null], [U(5)], [{ role: 'user' }], [{ role: 'user', content: null }], Array(51).fill(U('x'))]) {
      await assert.rejects(api.chat(bad), TypeError);
    }
    assert.equal(calls.length, 0);
    assert.throws(() => chatBody([{ role: 'system', content: 'x' }]), TypeError);
  });

  it('sends a stored API key in the header only, never in the URL or the body', async () => {
    const { api, calls } = setup(() => reply(answer()));
    api.setApiKey(KEY);
    await api.chat([U('hi')]);
    assert.equal(calls[0].init.headers['X-API-Key'], KEY);
    assert.ok(!calls[0].url.includes(KEY) && !calls[0].init.body.includes(KEY));
    const without = setup(() => reply(answer()));
    await without.api.chat([U('hi')]);
    assert.ok(!('X-API-Key' in without.calls[0].init.headers));
  });

  it('never writes the conversation or any key of its own to storage', async () => {
    const store = storage();
    const { api } = setup(() => reply(answer()), { storage: store });
    await api.chat([U('private question')]);
    assert.deepEqual(store.data, {});
  });
});

// ---------------------------------------------------------------- the key prompt
describe('a 401', () => {
  it('asks for a key once, retries once with it, and does not loop on a wrong key', async () => {
    let prompts = 0;
    const holder = {};
    const { api, calls } = setup(({ init }) => (init.headers['X-API-Key'] === KEY ? reply(answer()) : problem('unauthorized', 401)), {
      onAuthRequired: async () => { prompts += 1; return holder.api.setApiKey(KEY); } });
    holder.api = api;
    const out = await api.chat([U('hi')]);
    assert.equal(out.status, 'answered');
    assert.equal(prompts, 1);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].init.body, calls[0].init.body);
    const wrong = setup(() => problem('unauthorized', 401), { onAuthRequired: async () => { prompts += 1; return false; } });
    await assert.rejects(wrong.api.chat([U('hi')]), e => e.kind === 'auth');
    assert.equal(wrong.calls.length, 1);
  });
});

// ---------------------------------------------------------------- errors
describe('errors', () => {
  const cases = [
    [() => problem('unauthorized', 401), 'auth', 401, 'API key required'],
    [() => problem('forbidden-origin', 403), 'forbidden', 403, 'Request refused'],
    [() => problem('payload-too-large', 413), 'client', 413, 'Message too long'],
    [() => problem('validation-error', 422), 'validation', 422, 'Invalid request'],
    [() => problem('rate-limited', 429, { 'Retry-After': '17' }), 'rate-limit', 429, 'Too many questions'],
    [() => problem('budget-exhausted', 429, { 'Retry-After': '3600' }), 'rate-limit', 429, 'Daily analyst limit reached'],
    [() => problem('analyst-upstream-error', 502), 'server', 502, 'The analyst could not answer'],
    [() => problem('analyst-not-configured', 503), 'unavailable', 503, 'Analyst not configured'],
    [() => problem('analyst-timeout', 504), 'server-timeout', 504, 'The analyst took too long'],
    [() => problem('internal-error', 500), 'server', 500, 'Server error'],
  ];
  for (const [make, kind, status, title] of cases) {
    it(`${status} is kind "${kind}" and reads "${title}"`, async () => {
      const { api } = setup(() => make());
      await assert.rejects(api.chat([U('hi')]), err => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.kind, kind);
        assert.equal(err.status, status);
        assert.equal(describeError(err).title, title);
        assert.equal(err.requestId, 'rid-9');
        return true;
      });
    });
  }

  it('keeps Retry-After for a 429', async () => {
    const { api } = setup(() => problem('rate-limited', 429, { 'Retry-After': '17' }));
    await assert.rejects(api.chat([U('hi')]), e => e.retryAfterS === 17);
  });

  it('a 503 for "not configured" is recognisable by its slug', async () => {
    const { api } = setup(() => problem('analyst-not-configured', 503));
    await assert.rejects(api.chat([U('hi')]), e => e.type === 'analyst-not-configured' && e.kind === 'unavailable');
  });

  it('a network failure, a timeout and a cancel are told apart', async () => {
    const down = setup(() => { throw new TypeError('Failed to fetch'); });
    await assert.rejects(down.api.chat([U('hi')]), e => e.kind === 'network');
    const slow = setup(({ init }) => hang(init.signal), { chatTimeoutMs: 20 });
    await assert.rejects(slow.api.chat([U('hi')]), e => e.kind === 'timeout' && e.retryable);
    const controller = new AbortController();
    const waiting = setup(({ init }) => hang(init.signal));
    const pending = waiting.api.chat([U('hi')], { signal: controller.signal });
    controller.abort();
    await assert.rejects(pending, e => e.kind === 'aborted');
  });

  it('never echoes a non-JSON error body or exception text', async () => {
    const { api } = setup(() => new Response('<html><script>alert(1)</script>Traceback (most recent call last)</html>',
                                             { status: 500, headers: { 'Content-Type': 'text/html' } }));
    await assert.rejects(api.chat([U('hi')]), err => {
      assert.equal(err.kind, 'server');
      assert.ok(!/script|Traceback|html/i.test(JSON.stringify([err.message, err.detail, describeError(err)])));
      return true;
    });
  });

  it('a success that is not JSON, or not the promised shape, is a parse error', async () => {
    const text = setup(() => new Response('hello', { status: 200, headers: { 'Content-Type': 'text/plain' } }));
    await assert.rejects(text.api.chat([U('hi')]), e => e.kind === 'parse');
    const broken = setup(() => new Response('{not json', { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await assert.rejects(broken.api.chat([U('hi')]), e => e.kind === 'parse');
    for (const bad of [{}, [], null, answer({ status: 'maybe' }), answer({ answer: '' }), answer({ answer: 5 }),
                       answer({ status: 'withheld', answer: 'text' }), answer({ grounding: null }),
                       answer({ grounding: grounding({ status: 'great' }) }), answer({ sources: 'x' })]) {
      const { api } = setup(() => reply(bad));
      await assert.rejects(api.chat([U('hi')]), e => e.kind === 'parse', JSON.stringify(bad));
    }
  });
});

// ---------------------------------------------------------------- reading an answer
describe('readChatResponse', () => {
  it('keeps only the fields the contract defines, renamed for the page', () => {
    const out = readChatResponse(answer({ service: 'api.services.x', tool_trace: [1], system: 'x',
      sources: [source({ service: 'api.services.overview.build', notice: 'internal' })] }), 'hdr', 200);
    assert.deepEqual(Object.keys(out).sort(), ['answer', 'dataset', 'grounding', 'notice', 'reason', 'requestId', 'sources', 'status', 'usage']);
    assert.deepEqual(Object.keys(out.sources[0]).sort(), ['arguments', 'asOf', 'caveats', 'dataVersion', 'endpoint', 'id', 'relations', 'supportedClaims', 'tool', 'truncated']);
    assert.ok(!JSON.stringify(out).includes('api.services') && !/tool_trace|internal/.test(JSON.stringify(out)));
    assert.equal(out.grounding.claimsChecked, 2);
    assert.equal(out.sources[0].dataVersion, 'run-7');
  });

  it('a withheld answer has no text and carries the notice', () => {
    const out = readChatResponse(answer({ status: 'withheld', answer: null, reason: 'ungrounded', notice: 'Withheld.',
      grounding: grounding({ status: 'rejected', unverified_count: 2 }) }), null, 200);
    assert.equal(out.answer, null);
    assert.equal(out.notice, 'Withheld.');
  });

  it('caps the sources and ignores wrong types inside them', () => {
    const many = readChatResponse(answer({ sources: Array.from({ length: 40 }, () => source()) }), null, 200);
    assert.equal(many.sources.length, 20);
    const odd = readChatResponse(answer({ sources: [null, 'x', source({ arguments: [1], caveats: [1, 'ok'], truncated: 'yes', supported_claims: -3 })] }), null, 200);
    assert.equal(odd.sources.length, 1);
    assert.deepEqual(odd.sources[0].arguments, {});
    assert.deepEqual(odd.sources[0].caveats, ['ok']);
    assert.equal(odd.sources[0].truncated, false);
    assert.equal(odd.sources[0].supportedClaims, 0);
  });
});

// ---------------------------------------------------------------- history sent with a question
describe('chatWindow', () => {
  it('is the earlier answered turns plus the new question, as fresh {role, content} objects', () => {
    const history = [U('a'), A('b')];
    const out = M.chatWindow(history, '  next question ');
    assert.deepEqual(out, [U('a'), A('b'), U('next question')]);
    out[0].content = 'changed';
    assert.equal(history[0].content, 'a');
  });
  it('sends at most the last 8 earlier messages (the API takes 10 in all) and still starts with a user message', () => {
    const history = Array.from({ length: 12 }, (_, i) => (i % 2 ? A('a' + i) : U('u' + i)));
    const out = M.chatWindow(history, 'q');
    assert.equal(M.CHAT_HISTORY_MESSAGES, 8);
    assert.equal(out.length, 9);
    assert.deepEqual(out.map(m => m.role), ['user', 'assistant', 'user', 'assistant', 'user', 'assistant', 'user', 'assistant', 'user']);
    assert.equal(out[0].content, 'u4');
  });
  it('an empty or non-text question is refused', () => {
    for (const bad of ['', '   ', null, undefined, 5, {}]) assert.throws(() => M.chatWindow([], bad), TypeError);
  });
});

// ---------------------------------------------------------------- describing an answer
describe('chatAnswerModel', () => {
  const model = body => M.chatAnswerModel(readChatResponse(body, null, 200));

  it('a verified answer is shown with the API\'s own counts and its sources', () => {
    const m = model(answer());
    assert.equal(m.kind, 'answered');
    assert.equal(m.text, 'MRR is $61,870.');
    assert.deepEqual(m.badge, { tone: 'good', text: 'Verified: 2 figures matched to the sources (1 computed from them)' });
    assert.equal(m.sourcesLabel, 'Sources (1)');
    assert.deepEqual(m.sources[0], { title: 'get_overview', endpoint: '/api/overview', filters: 'no filters',
      version: 'data version run-7 · as of 2025-12-31', relations: 'Tables: gold.fct_kpis', caveats: [], truncated: '',
      supports: 'Supports 2 figures in the answer' });
    assert.equal(m.dataset, 'synthetic');
  });
  it('a withheld answer shows the notice, not an answer, and a red badge', () => {
    const m = model(answer({ status: 'withheld', answer: null, reason: 'ungrounded', notice: 'Some numbers could not be matched.',
      grounding: grounding({ status: 'rejected', unverified_count: 1 }), sources: [source({ supported_claims: 0 })] }));
    assert.equal(m.kind, 'withheld');
    assert.equal(m.text, 'Some numbers could not be matched.');
    assert.deepEqual(m.badge, { tone: 'bad', text: 'Withheld: 1 figure could not be matched to the data' });
    assert.equal(m.sources[0].supports, 'Consulted');
  });
  it('not_checked and an answer with no figures are described as the API reports them, nothing more', () => {
    assert.deepEqual(model(answer({ status: 'withheld', answer: null, reason: 'tool-limit', notice: 'Too many lookups.',
      grounding: grounding({ status: 'not_checked', claims_checked: 0 }), sources: [] })).badge,
      { tone: 'neutral', text: 'Not checked: no answer was produced' });
    const none = model(answer({ grounding: grounding({ claims_checked: 0, claims_derived: 0 }), sources: [] }));
    assert.equal(none.badge.text, 'Checked: the answer contains no figures to verify');
    assert.equal(none.sourcesLabel, 'Sources: no data was looked up');
    assert.deepEqual([M.groundingBadge({ status: 'verified', claimsChecked: 1, claimsDerived: 0 }).text], ['Verified: 1 figure matched to the sources']);
  });
  it('caveats become plain sentences, unknown ones are shown as text, duplicates once', () => {
    const m = model(answer({ grounding: grounding({ caveats: ['truncated-evidence', 'truncated-evidence', '<b>odd</b>'],
      dates_not_in_evidence: ['2030-01-01'] }), sources: [source({ truncated: true, caveats: ['2025-12 is incomplete.'], arguments: { plan_tier: 'pro', features: ['sms', 'voice_calls'] } })] }));
    assert.deepEqual(m.notes, ['Some source data was shortened to fit size limits, so totals over it were not used.', '<b>odd</b>',
                               'Not found in the sources: 2030-01-01.']);
    assert.equal(m.sources[0].filters, 'features = sms, voice_calls; plan_tier = pro');
    assert.equal(m.sources[0].truncated, 'Some rows were left out of this result to fit a size limit.');
    assert.deepEqual(m.sources[0].caveats, ['2025-12 is incomplete.']);
  });
  it('model and warehouse text stays an inert string', () => {
    const evil = '<img src=x onerror="window.__pwned=1"><script>alert(1)</script>';
    const m = model(answer({ answer: evil, sources: [source({ tool: evil, endpoint: evil, caveats: [evil], relations: [evil] })] }));
    assert.equal(m.text, evil);
    assert.equal(typeof m.sources[0].title, 'string');
  });
});

// ---------------------------------------------------------------- the page code
describe('the page\'s analyst code', () => {
  const code = analystSource.replace(/\/\/.*$/gm, '');
  it('uses the shared client for its one request and no other network or storage', () => {
    assert.equal((code.match(/needApi\(\)\.chat\(/g) || []).length, 1);
    for (const forbidden of [/\bfetch\(/, /XMLHttpRequest/, /sendBeacon/, /WebSocket/, /EventSource/, /localStorage/, /sessionStorage/,
                             /document\.cookie/, /\blocation\b/, /history\.(push|replace)State/, /indexedDB/, /console\./, /window\.open/]) {
      assert.ok(!forbidden.test(code), String(forbidden));
    }
  });
  it('writes text with textContent only: no HTML sink, no style attribute', () => {
    for (const sink of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/, /new Function/, /setAttribute\(\s*['"]style/, /srcdoc/, /createContextualFragment/, /DOMParser/]) {
      assert.ok(!sink.test(code), String(sink));
    }
    assert.ok(/textContent = /.test(code) || /node\('/.test(code));
  });
  it('holds the conversation in memory and sends only what chatWindow built', () => {
    assert.match(code, /let history = \[\];/);
    assert.match(code, /chatWindow\(history, text\)/);
    assert.ok(!/role:\s*['"](system|developer|tool|function)/.test(code));
  });
  it('the page names no credential or provider', () => {
    assert.ok(!/gemini|api[_-]?key\s*[:=]\s*['"]/i.test(code));
    assert.ok(!/gemini/i.test(html));
  });
});
