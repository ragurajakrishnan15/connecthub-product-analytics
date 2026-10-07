"""The dashboard's Analyst tab in a real browser (Playwright + Chromium), PHASE_6_PLAN.md step 6.

The page is the real index.html served by the real API under its real Content-Security-Policy. The
model is the scripted fake from tests/api/analyst_testlib.py (no Gemini, no key, no network beyond
127.0.0.1) and the database is dead, so nothing here needs PostgreSQL: the analyst's tools are
stubbed in-process and the other dashboard panels are fed trimmed real API responses from
tests/dashboard/fixtures. Any uncaught page error, CSP violation or request to a host the page must
not use fails the test (tests/browser/browserlib.py). Skipped when Playwright or Chromium is not available.
"""
import importlib
import json
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip('playwright.sync_api')

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests' / 'api'))          # the scripted fake model and fake engine

from analyst_testlib import ScriptedLlm, Service, call, envelope, say, use  # noqa: E402
from browserlib import INDEX, RunningApp, _dash, _free_port  # noqa: E402,F401
from playwright.sync_api import expect  # noqa: E402

from api.analyst import tools  # noqa: E402
from api.settings import Settings  # noqa: E402

pytestmark = [pytest.mark.browser]
expect.set_options(timeout=20_000)

FIXTURES = ROOT / 'tests' / 'dashboard' / 'fixtures'
FAKE_KEY = 'fake-gemini-key-for-tests-0123456789'
OVERVIEW_DATA = {'mrr': {'value': 61870.0, 'previous': 58200.0}, 'activation': {'rate': 0.639}}
CHAT = '**/api/analyst/chat'


# ---------------------------------------------------------------- apps (no warehouse needed)
def analyst_settings(port, **overrides):
    values = {'api_db_password': 'not-a-real-password-123', 'POSTGRES_PORT': 1, 'api_db_connect_timeout_s': 1,
              'api_dashboard_path': str(INDEX), 'api_env': 'development',
              'api_cors_origins': [f'http://127.0.0.1:{port}'], 'analyst_enabled': True,
              'gemini_api_key': FAKE_KEY, 'analyst_model': 'fake-model', 'analyst_rate_limit_per_min': 600,
              'analyst_daily_token_budget': 10_000_000, **overrides}
    return Settings(**values)


def start(**overrides):
    port = _free_port()
    return RunningApp(analyst_settings(port, **overrides), port=port, analyst_llm=ScriptedLlm()).start()


@pytest.fixture(scope='module')
def analyst_app():
    running = start()
    yield running
    running.stop()


@pytest.fixture(scope='module')
def disabled_app():
    running = start(analyst_enabled=False)
    yield running
    running.stop()


@pytest.fixture(scope='module')
def keyed_app():
    key = secrets.token_urlsafe(24)
    port = _free_port()
    running = RunningApp(analyst_settings(port, api_auth_mode='api_key', api_keys=[key]), port=port, key=key,
                         analyst_llm=ScriptedLlm()).start()
    yield running
    running.stop()


@pytest.fixture
def dash(chromium, analyst_app):
    yield from _dash(chromium, analyst_app)


@pytest.fixture
def dash_disabled(chromium, disabled_app):
    yield from _dash(chromium, disabled_app)


@pytest.fixture
def dash_keyed(chromium, keyed_app):
    yield from _dash(chromium, keyed_app)


def script(d, *steps):
    """Give the app a fresh scripted model for this test."""
    llm = ScriptedLlm(*steps)
    d.app.app.state.analyst_llm = llm
    return llm


def stub_overview(monkeypatch, data=OVERVIEW_DATA):
    @contextmanager
    def no_database(engine):                     # the stubbed service ignores its connection
        yield None
    monkeypatch.setattr(tools, 'read_only_connection', no_database)
    module, function = tools.TOOLS['get_overview'].service.rsplit('.', 1)
    service = Service(envelope(data))
    monkeypatch.setattr(importlib.import_module(module), function, service)
    return service


def open_analyst(d):
    d.open()
    d.tab('ai')
    expect(d.page.locator('#tab-ai')).to_be_visible()
    return d.page


def ask(page, text):
    page.fill('#ai-input', text)
    page.click('#ai-send')


def chat_posts(d):
    return [r for r in d.watch.requests if r[0] == 'POST' and r[1].endswith('/api/analyst/chat')]


def stub_chat(page, status=200, body=None, content_type='application/json', headers=None):
    page.route(CHAT, lambda route: route.fulfill(status=status, body=json.dumps(body), headers=headers or {},
                                                  content_type=content_type))


def problem(slug, status, **extra):
    return {'type': f'urn:connecthub:problem:{slug}', 'title': 'T', 'status': status, 'detail': 'detail text',
            'instance': '/api/analyst/chat', 'request_id': 'rid-42', **extra}


def answer_body(**over):
    body = {'status': 'answered', 'answer': 'MRR is $61,870.', 'format': 'text/plain', 'reason': None, 'notice': None,
            'grounding': {'status': 'verified', 'claims_checked': 1, 'claims_derived': 0, 'unverified_count': 0,
                          'caveats': [], 'dates_not_in_evidence': []},
            'sources': [{'id': 'call-1', 'tool': 'get_overview', 'endpoint': '/api/overview', 'arguments': {},
                         'data_version': 'run-7', 'as_of': '2025-12-31', 'relations': ['gold.fct_kpis'],
                         'caveats': [], 'truncated': False, 'supported_claims': 1}],
            'usage': {'requests': 2, 'tool_calls': 1, 'total_tokens': 67}, 'dataset': 'synthetic',
            'request_id': 'rid-1'}
    body.update(over)
    return body


# ================================================================ navigation and the empty state
def test_the_analyst_tab_is_in_the_navigation_and_opens(dash):
    page = open_analyst(dash)
    assert page.locator('.nav-tab[data-tab="ai"]').text_content().strip() == 'AI Analyst'
    expect(page.locator('.nav-tab[data-tab="ai"]')).to_have_class('nav-tab active')
    assert page.locator('#tab-overview').is_hidden()
    text = page.locator('#tab-ai').inner_text()
    for promised in ('Analyst', 'checked against', 'withheld', 'lists its sources', 'may be unavailable',
                     'not saved', 'No questions yet'):
        assert promised in text, promised
    assert page.locator('#ai-send').is_enabled() and page.locator('#ai-input').is_editable()
    assert page.locator('#ai-new').is_hidden() and page.locator('#ai-notice').is_hidden()
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'idle')


def test_opening_the_tab_sends_nothing_to_the_analyst(dash):
    page = open_analyst(dash)
    page.wait_for_timeout(300)
    assert not [p for p in dash.watch.api_paths() if p.startswith('api/analyst')]
    assert not chat_posts(dash)


def test_navigation_between_tabs_still_works(dash):
    dash.open()
    for name in ('retention', 'funnel', 'experiments', 'health', 'ai', 'overview'):
        dash.tab(name)
        expect(dash.page.locator(f'#tab-{name}')).to_be_visible()
        assert dash.page.locator('.tab-content.active').count() == 1
        assert dash.page.locator('.nav-tab.active').get_attribute('data-tab') == name


# ================================================================ a grounded answer
def test_a_grounded_answer_is_shown_with_its_badge_and_sources(dash, monkeypatch):
    stub_overview(monkeypatch)
    llm = script(dash, use(call('get_overview')), say('MRR is $61,870, up 6.3%.'))
    page = open_analyst(dash)
    ask(page, 'What is MRR?')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    assert page.locator('#ai-messages .ai-msg.user').text_content() == 'What is MRR?'
    assert page.locator('#ai-messages .ai-msg.ai').text_content() == 'MRR is $61,870, up 6.3%.'
    badge = page.locator('#ai-messages .ai-badge')
    assert badge.get_attribute('class') == 'ai-badge good'
    assert badge.text_content() == 'Verified: 2 figures matched to the sources (1 computed from them)'
    summary = page.locator('#ai-messages .ai-sources summary')
    assert summary.text_content() == 'Sources (1)'
    summary.click()
    source = page.locator('#ai-messages .ai-sources li').inner_text()
    for shown in ('get_overview', '/api/overview', 'no filters', 'data version v-test', 'as of 2025-12-31',
                  'Tables: gold.fake_table', 'Supports 2 figures'):
        assert shown in source, shown
    assert 'api.services' not in page.locator('#tab-ai').inner_text()            # server-side detail stays private
    assert page.locator('#ai-dataset').text_content() == 'Dataset: synthetic'
    assert page.locator('#ai-input').input_value() == '' and page.locator('#ai-new').is_visible()
    assert llm.calls == 2


def test_the_request_is_the_supported_shape_and_leaks_nothing(dash, monkeypatch):
    stub_overview(monkeypatch)
    script(dash, say('The dashboard data does not show that.'))
    page = open_analyst(dash)
    ask(page, 'Is this & that #1 secret?')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    ((method, url, key),) = chat_posts(dash)
    assert url == dash.app.origin + '/api/analyst/chat' and key is None
    posted = page.evaluate("() => performance.getEntriesByType('resource').map(e => e.name)")
    assert [u for u in posted if '/api/analyst/chat' in u] == [dash.app.origin + '/api/analyst/chat']
    assert not [u for u in posted if 'secret' in u]
    assert page.url.endswith('/') and '?' not in page.url and '#' not in page.url


def test_multi_turn_history_sends_only_user_and_assistant_text(dash, monkeypatch):
    stub_overview(monkeypatch)
    llm = script(dash, use(call('get_overview')), say('MRR is $61,870.'), say('Anything else?'))
    page = open_analyst(dash)
    ask(page, 'What is MRR?')
    expect(page.locator('#ai-messages .ai-turn')).to_have_count(1)
    ask(page, 'And now?')
    expect(page.locator('#ai-messages .ai-turn')).to_have_count(2)
    # the model saw: turn 1 = [user]; turn 2 = [user, assistant, user]. No system, tool or extra messages.
    last = llm.requests[-1].messages
    assert [m['role'] for m in last] == ['user', 'assistant', 'user']
    assert [m['text'] for m in last] == ['What is MRR?', 'MRR is $61,870.', 'And now?']
    assert llm.requests[0].messages == ({'role': 'user', 'text': 'What is MRR?'},)
    assert page.locator('#ai-messages .ai-msg.user').count() == 2


def test_a_withheld_answer_is_marked_and_its_figures_are_not_shown(dash, monkeypatch):
    stub_overview(monkeypatch)
    llm = script(dash, use(call('get_overview')), say('MRR is $99,999.'), say('No figures here.'))
    page = open_analyst(dash)
    ask(page, 'What is MRR?')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    turn = page.locator('#ai-messages .ai-turn')
    assert 'withheld' in turn.locator('.ai-msg.ai').get_attribute('class')
    assert turn.locator('.ai-badge').get_attribute('class') == 'ai-badge bad'
    assert turn.locator('.ai-badge').text_content() == 'Withheld: 1 figure could not be matched to the data'
    assert 'could not be matched' in turn.locator('.ai-msg.ai').text_content()
    assert '99,999' not in page.locator('body').inner_text()
    # a withheld answer is not carried into the next question's history
    ask(page, 'Try again')
    expect(page.locator('#ai-messages .ai-turn')).to_have_count(2)
    assert [m['text'] for m in llm.requests[-1].messages] == ['Try again']


def test_new_conversation_clears_the_screen_and_the_history(dash, monkeypatch):
    llm = script(dash, say('First.'), say('Second.'))
    page = open_analyst(dash)
    ask(page, 'one')
    expect(page.locator('#ai-messages .ai-turn')).to_have_count(1)
    page.click('#ai-new')
    assert page.locator('#ai-messages .ai-msg').count() == 0 and page.locator('#ai-empty').is_visible()
    assert page.locator('#ai-new').is_hidden()
    ask(page, 'two')
    expect(page.locator('#ai-messages .ai-turn')).to_have_count(1)
    assert [m['text'] for m in llm.requests[-1].messages] == ['two']


# ================================================================ loading and keyboard
def test_the_send_control_is_disabled_and_the_loading_state_shown_while_a_request_is_active(dash):
    held = []
    dash.page.route(CHAT, lambda route: held.append(route))
    page = open_analyst(dash)
    ask(page, 'slow question')
    panel = page.locator('#panel-analyst')
    expect(panel).to_have_attribute('data-state', 'loading')
    assert panel.get_attribute('aria-busy') == 'true'
    assert page.locator('#ai-send').is_disabled() and page.locator('#ai-input').get_attribute('readonly') is not None
    assert 'Looking up the data' in page.locator('#ai-messages .thinking').text_content()
    page.press('#ai-input', 'Enter')                                   # no second request while one is active
    page.wait_for_timeout(200)
    assert len(held) == 1
    held[0].fulfill(status=200, body=json.dumps(answer_body()), content_type='application/json')
    expect(panel).to_have_attribute('data-state', 'ready')
    assert page.locator('#ai-send').is_enabled() and page.locator('#ai-messages .thinking').count() == 0
    assert page.locator('#ai-messages .ai-msg.ai').text_content() == 'MRR is $61,870.'


def test_enter_sends_and_shift_enter_adds_a_line(dash):
    script(dash, say('Fine.'))
    page = open_analyst(dash)
    page.click('#ai-input')
    page.keyboard.type('line one')
    page.keyboard.press('Shift+Enter')
    page.keyboard.type('line two')
    assert page.locator('#ai-input').input_value() == 'line one\nline two'
    assert not chat_posts(dash)
    page.keyboard.press('Enter')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    assert len(chat_posts(dash)) == 1
    assert page.locator('#ai-messages .ai-msg.user').text_content() == 'line one\nline two'


def test_an_empty_question_sends_nothing(dash):
    page = open_analyst(dash)
    page.fill('#ai-input', '   ')
    page.click('#ai-send')
    page.press('#ai-input', 'Enter')
    page.wait_for_timeout(200)
    assert not chat_posts(dash) and page.locator('#ai-messages .ai-msg').count() == 0


# ================================================================ failures
ERRORS = [
    (403, problem('forbidden-origin', 403), 'Request refused', {}),
    (413, problem('payload-too-large', 413), 'Message too long', {}),
    (422, problem('validation-error', 422), 'Invalid request', {}),
    (429, problem('rate-limited', 429), 'Too many questions', {'Retry-After': '17'}),
    (429, problem('budget-exhausted', 429), 'Daily analyst limit reached', {'Retry-After': '3600'}),
    (500, problem('internal-error', 500), 'Server error', {}),
    (502, problem('analyst-upstream-error', 502), 'The analyst could not answer', {}),
    (504, problem('analyst-timeout', 504), 'The analyst took too long', {}),
]


@pytest.mark.parametrize('status, body, title, headers', ERRORS, ids=[f'{e[0]}-{e[2]}' for e in ERRORS])
def test_a_failure_is_explained_and_the_question_comes_back(dash, status, body, title, headers):
    stub_chat(dash.page, status, body, 'application/problem+json', headers)
    page = open_analyst(dash)
    ask(page, 'my question')
    notice = page.locator('#ai-notice')
    expect(notice).to_be_visible()
    assert notice.locator('.state-title').text_content() == title
    assert 'request rid-42' in notice.inner_text()
    if headers.get('Retry-After'):
        assert f"try again in {headers['Retry-After']} seconds" in notice.inner_text()
    assert notice.get_attribute('role') == 'alert'
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'error')
    assert page.locator('#ai-input').input_value() == 'my question'      # nothing was answered: ask again
    assert page.locator('#ai-send').is_enabled()
    assert page.locator('#ai-messages .ai-msg').count() == 0 and page.locator('#ai-empty').is_visible()
    assert ('detail text' in page.locator('#ai-notice').inner_text()) == (status == 422)   # only a validation detail is shown


def test_a_503_not_configured_disables_the_box_until_a_new_conversation(dash):
    stub_chat(dash.page, 503, problem('analyst-not-configured', 503), 'application/problem+json')
    page = open_analyst(dash)
    ask(page, 'hello?')
    notice = page.locator('#ai-notice')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'unavailable')
    assert notice.locator('.state-title').text_content() == 'Analyst not configured'
    assert 'switched off or not set up' in notice.inner_text()
    assert page.locator('#ai-input').is_disabled() and page.locator('#ai-send').is_disabled()
    page.unroute(CHAT)
    page.click('#ai-new')
    assert page.locator('#ai-input').is_enabled() and page.locator('#ai-send').is_enabled()
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'idle')


def test_the_real_server_says_not_configured_while_the_analyst_is_disabled(dash_disabled):
    page = open_analyst(dash_disabled)
    ask(page, 'Is anyone there?')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'unavailable')
    assert page.locator('#ai-notice .state-title').text_content() == 'Analyst not configured'
    assert page.locator('#ai-input').is_disabled()
    assert FAKE_KEY not in page.content()


def test_a_network_failure_and_a_malformed_response_are_explained(dash):
    dash.page.route(CHAT, lambda route: route.abort())
    page = open_analyst(dash)
    ask(page, 'q1')
    assert page.locator('#ai-notice .state-title').text_content() == 'Cannot reach the API'
    assert page.locator('#ai-input').input_value() == 'q1'
    page.unroute(CHAT)
    stub_chat(page, 200, {'unexpected': True})
    page.click('#ai-send')
    expect(page.locator('#ai-notice .state-title')).to_have_text('Unexpected response')
    assert page.locator('#ai-messages .ai-msg').count() == 0
    page.unroute(CHAT)
    stub_chat(page, 200, answer_body())
    page.click('#ai-send')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    assert page.locator('#ai-notice').is_hidden()


def test_a_401_asks_for_a_key_and_cancel_leaves_a_clear_error(dash):
    stub_chat(dash.page, 401, problem('unauthorized', 401), 'application/problem+json')
    page = open_analyst(dash)
    ask(page, 'hi')
    dialog = page.locator('dialog.key-dialog')
    expect(dialog).to_be_visible()
    dialog.get_by_role('button', name='Cancel').click()
    expect(page.locator('#ai-notice .state-title')).to_have_text('API key required')
    assert page.locator('#ai-input').input_value() == 'hi'


# ================================================================ keys and storage
def test_with_an_api_key_the_key_goes_in_a_header_only_and_the_conversation_is_never_stored(dash_keyed, monkeypatch):
    stub_overview(monkeypatch)
    script(dash_keyed, say('The dashboard data does not show that.'))
    d = dash_keyed
    key = d.app.key
    d.open()                                                    # the overview panels ask for the key first
    dialog = d.page.locator('dialog.key-dialog')
    expect(dialog).to_be_visible()
    dialog.locator('input').fill(key)
    dialog.get_by_role('button', name='Use key').click()
    expect(dialog).to_have_count(0)
    d.tab('ai')
    ask(d.page, 'a very private question')
    expect(d.page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    posts = chat_posts(d)
    assert [p[2] for p in posts] == [key]                       # the key travels in the header, once
    assert all(key not in url for _, url, _ in d.watch.requests)
    stored = d.page.evaluate("""() => ({ session: Object.entries(sessionStorage), local: Object.entries(localStorage),
        cookie: document.cookie, url: location.href, text: document.documentElement.innerText })""")
    assert stored['session'] == [['connecthub.apiKey', key]]
    assert stored['local'] == [] and stored['cookie'] == '' and '?' not in stored['url']
    assert 'a very private question' not in json.dumps(stored['session'] + stored['local'] + [stored['cookie'], stored['url']])
    assert key not in stored['text'] and FAKE_KEY not in stored['text'] and FAKE_KEY not in d.page.content()


# ================================================================ text only, no markup
def test_model_and_source_text_is_shown_as_text_and_never_run(dash):
    evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script><b>bold</b>'
    body = answer_body(answer=evil, notice=evil,
                       grounding={'status': 'verified', 'claims_checked': 0, 'claims_derived': 0, 'unverified_count': 0,
                                  'caveats': [evil], 'dates_not_in_evidence': [evil]},
                       sources=[{'id': 'c', 'tool': evil, 'endpoint': evil, 'arguments': {evil: evil}, 'data_version': evil,
                                 'as_of': evil, 'relations': [evil], 'caveats': [evil], 'truncated': True,
                                 'supported_claims': 1}])
    stub_chat(dash.page, 200, body)
    page = open_analyst(dash)
    ask(page, evil)
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    assert page.locator('#ai-messages .ai-msg.ai').text_content() == evil
    assert page.locator('#ai-messages .ai-msg.user').text_content() == evil
    page.locator('#ai-messages .ai-sources summary').click()
    assert page.locator('#ai-messages .ai-source-title').text_content() == evil
    assert evil in page.locator('#ai-messages .ai-notes').text_content()
    assert page.locator('#ai-messages img, #ai-messages script, #ai-messages b, #ai-notice b').count() == 0
    page.wait_for_timeout(300)
    assert page.evaluate('window.__pwned') is None
    assert not [u for _, u, _ in dash.watch.requests if u.endswith('/x')]       # the <img> never loaded


def test_a_long_unbroken_answer_does_not_widen_the_page(dash):
    stub_chat(dash.page, 200, answer_body(answer='x' * 3000 + ' ' + 'y' * 3000))
    page = open_analyst(dash)
    ask(page, 'long')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')


def test_the_tab_fits_a_narrow_phone_screen(dash):
    dash.page.set_viewport_size({'width': 375, 'height': 700})
    stub_chat(dash.page, 200, answer_body(answer='A reasonably long answer ' * 20))
    page = open_analyst(dash)
    ask(page, 'phone question')
    expect(page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
    for selector in ('#ai-input', '#ai-send', '#ai-messages .ai-turn'):
        box = page.locator(selector).bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= 376, selector


# ================================================================ policy and the rest of the dashboard
def test_the_policy_stays_strict_and_the_page_asks_only_its_own_origin(dash):
    response = dash.open()
    csp = response.headers['content-security-policy']
    for forbidden in ("'unsafe-inline'", "'unsafe-eval'", 'cdn.', 'unpkg', 'jsdelivr', 'cloudflare'):
        assert forbidden not in csp, forbidden
    assert "connect-src 'self'" in csp and "default-src 'none'" in csp
    assert 'script-src' in csp
    dash.tab('ai')
    stub_chat(dash.page, 200, answer_body())
    ask(dash.page, 'hello')
    expect(dash.page.locator('#panel-analyst')).to_have_attribute('data-state', 'ready')
    assert not dash.watch.external and not dash.watch.csp_reports()


FIXTURE_FOR = {'meta': 'meta', 'overview': 'overview', 'engagement': 'engagement_month', 'activation': 'activation',
               'cohorts': 'cohorts', 'revenue': 'revenue', 'feature-adoption': 'feature_adoption',
               'experiments': 'experiments', 'nps': 'nps', 'support': 'support_month',
               'customer-health': 'customer_health', 'customer-health/workspaces': 'workspaces'}


def serve_fixtures(page):
    def handler(route):
        path = route.request.url.split('/api/', 1)[1].split('?')[0]
        if path.startswith('experiments/'):
            name = 'experiment_aa' if 'aa' in path or path.endswith('v1') else 'experiment_ab'
        else:
            name = FIXTURE_FOR.get(path)
        if name is None or route.request.method != 'GET':
            return route.continue_()
        body = (FIXTURES / f'{name}.json').read_text(encoding='utf-8')
        return route.fulfill(status=200, body=body, content_type='application/json',
                             headers={'ETag': f'W/"{name}"'})
    page.route('**/api/**', handler)


def test_the_existing_panels_still_draw_from_api_responses(dash):
    serve_fixtures(dash.page)
    dash.open()
    for panel in ('panel-kpis', 'panel-dau', 'panel-adoption', 'panel-revenue', 'panel-agent', 'panel-nps-trend'):
        expect(dash.page.locator(f'#{panel}')).to_have_attribute('data-state', 'ready')
    assert dash.page.locator('.kpi-value').first.text_content() != '—'
    for tab, panel in (('retention', 'panel-cohorts'), ('funnel', 'panel-funnel'), ('health', 'panel-health-table')):
        dash.tab(tab)
        expect(dash.page.locator(f'#{panel}')).to_have_attribute('data-state', 'ready')
    dash.tab('ai')
    assert dash.page.locator('#ai-input').is_visible()
    dash.tab('overview')
    assert dash.page.locator('#panel-kpis').is_visible()
