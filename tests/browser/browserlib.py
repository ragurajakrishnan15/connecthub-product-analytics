"""Helpers for the dashboard browser tests (Playwright + Chromium).

The tests drive the real page against the real API and the local warehouse, the same way the
other integration tests do: the app is started in-process (a uvicorn thread) with the dashboard
enabled, so nothing depends on a running container. Everything skips when PostgreSQL, Playwright
or Chromium is not available (requirements/browser.txt).

Fixtures are imported by name into the test module (no conftest), like tests/api.
"""
import json
import os
import secrets
import socket
import threading
import time
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
# The page under test. CONNECTHUB_TEST_PAGE points the suite at another copy of it (with its vendor/
# directory next to it), e.g. to check that a deliberately broken page is caught.
INDEX = Path(os.environ.get('CONNECTHUB_TEST_PAGE') or ROOT / 'index.html')
FONT_PREFIXES = ('https://fonts.googleapis.com/', 'https://fonts.gstatic.com/')
CSP_MARKERS = ('Content Security Policy', 'Refused to ')


# ---------------------------------------------------------------- the app under test
class ThreadedServer(uvicorn.Server):
    def install_signal_handlers(self):          # not the main thread
        pass


class RunningApp:
    """A uvicorn server in a background thread, plus tiny helpers to read its API."""

    def __init__(self, settings, key=None):
        from api.main import create_app
        self.key = key
        self.port = _free_port()
        self.origin = f'http://127.0.0.1:{self.port}'
        config = uvicorn.Config(create_app(settings), host='127.0.0.1', port=self.port,
                                log_config=None, access_log=False, lifespan='on')
        self.server = ThreadedServer(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.time() > deadline:
                raise RuntimeError('the test server did not start')
            time.sleep(0.05)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)

    def get(self, path):
        """GET an API path, independently of the browser; returns (data, meta)."""
        request = urllib.request.Request(self.origin + path)
        if self.key:
            request.add_header('X-API-Key', self.key)
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
        return body['data'], body['meta']


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _settings(**overrides):
    """Settings for the local warehouse (skip if it is not configured and built), with the
    dashboard on. Nothing from a developer's API_* environment can change the mode."""
    from api.provision import provision
    from api.settings import Settings
    from pipeline import config
    from sqlalchemy import text

    if not os.environ.get('POSTGRES_PASSWORD') or not os.environ.get('API_DB_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD / API_DB_PASSWORD not set; no PostgreSQL configured')
    values = {'api_dashboard_path': str(INDEX), 'api_auth_mode': 'none', 'api_keys': [],
              'api_env': 'development', **overrides}
    settings = Settings(**values)
    owner = config.create_engine(settings.postgres_db)
    try:
        with owner.connect() as conn:
            conn.execute(text('SELECT 1 FROM gold.fct_activation_daily LIMIT 1'))
        provision(owner, settings.api_db_user, settings.api_db_password.get_secret_value())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built: {exc}')
    finally:
        owner.dispose()
    return settings


@pytest.fixture(scope='module')
def app():
    running = RunningApp(_settings()).start()
    yield running
    running.stop()


@pytest.fixture(scope='module')
def app_with_key():
    key = secrets.token_urlsafe(24)
    running = RunningApp(_settings(api_auth_mode='api_key', api_keys=[key]), key=key).start()
    yield running
    running.stop()


# ---------------------------------------------------------------- the browser
@pytest.fixture(scope='session')
def chromium():
    sync_api = pytest.importorskip('playwright.sync_api')
    playwright = sync_api.sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        playwright.stop()
        pytest.skip(f'Chromium is not available to Playwright (see requirements/browser.txt): '
                    f'{str(exc).splitlines()[0][:160]}')
    yield browser
    browser.close()
    playwright.stop()


class Watch:
    """What the browser did during one test."""

    def __init__(self):
        self.requests = []          # (method, url, x-api-key header or None)
        self.console = []           # (type, text)
        self.page_errors = []       # uncaught exceptions in the page
        self.external = []          # requests to hosts the page must not use
        self.allow_csp_reports = False

    def api_paths(self):
        return [url.split('//', 1)[1].split('/', 1)[1].split('?')[0] for _, url, _ in self.requests
                if '/api/' in url]

    def csp_reports(self):
        return [t for _, t in self.console if any(m in t for m in CSP_MARKERS)]


class Dash:
    """A page opened on the app under test, with everything it did recorded."""

    def __init__(self, page, watch, app):
        self.page, self.watch, self.app = page, watch, app

    def open(self, path='/'):
        return self.page.goto(self.app.origin + path)

    def tab(self, name):
        self.page.click(f'.nav-tab[data-tab="{name}"]')

    def chart(self, canvas_id):
        """Labels and datasets of a Chart.js chart, as the page drew them."""
        return self.page.evaluate("""id => {
            const c = Chart.getChart(document.getElementById(id));
            return c && { type: c.config.type, labels: c.data.labels,
              datasets: c.data.datasets.map(d => ({ label: d.label, data: d.data, backgroundColor: d.backgroundColor })) };
        }""", canvas_id)


@pytest.fixture
def dash(chromium, app):
    yield from _dash(chromium, app)


@pytest.fixture
def dash_with_key(chromium, app_with_key):
    yield from _dash(chromium, app_with_key)


def _dash(chromium, app):
    context = chromium.new_context(viewport={'width': 1400, 'height': 1000}, locale='en-US',
                                   timezone_id='UTC', service_workers='block')
    watch = Watch()

    def outside(url):
        return url.startswith(('http:', 'https:')) and not url.startswith(app.origin)

    def answer(route):
        url = route.request.url
        if url.startswith(FONT_PREFIXES):       # allowed hosts: answer offline, with no font files
            route.fulfill(status=200, content_type='text/css', body='')
        else:
            watch.external.append(url)
            route.abort()
    context.route(outside, answer)
    page = context.new_page()
    page.on('request', lambda r: watch.requests.append((r.method, r.url, r.headers.get('x-api-key'))))
    page.on('console', lambda m: watch.console.append((m.type, m.text)))
    page.on('pageerror', lambda e: watch.page_errors.append(str(e)))
    yield Dash(page, watch, app)
    context.close()
    assert not watch.page_errors, f'uncaught errors in the page: {watch.page_errors}'
    assert not watch.external, f'the page requested hosts it must not use: {watch.external}'
    if not watch.allow_csp_reports:
        assert not watch.csp_reports(), f'Content-Security-Policy violations: {watch.csp_reports()}'


# ---------------------------------------------------------------- expected values, formatted independently of the page
def round_half_up(value):
    return int(Decimal(repr(float(value))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt_int(value):
    return f'{round_half_up(value):,}'


def fmt_pct(rate, digits=1):
    return f'{rate * 100:.{digits}f}%'


def scaled(value, factor=100):
    return None if value is None else value * factor


def approx_list(values):
    """pytest.approx for a list that may contain None (a gap in a chart)."""
    return [None if v is None else pytest.approx(v, rel=1e-12, abs=1e-12) for v in values]
