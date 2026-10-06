"""The dashboard page served at GET / (PHASE_5_PLAN.md step 1): serving, the derived
Content-Security-Policy, startup refusals, and that the API contract is untouched.
No database is needed."""
import base64
import hashlib
import re
from pathlib import Path

import pytest
from api_testlib import (FAKE_PASSWORD, assert_problem, clean_env,  # noqa: F401
                         client_for, make_settings)

from api.dashboard import DashboardError, load_dashboard
from api.main import create_app

pytestmark = pytest.mark.usefixtures('clean_env')

INDEX = Path(__file__).resolve().parents[2] / 'index.html'
CHART_JS = 'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js'


def sha(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def directive(csp, name):
    for part in csp.split(';'):
        if part.strip().split(' ')[0] == name:
            return part.strip().split(' ')[1:]
    raise AssertionError(f'{name} not in {csp}')


def page_text():
    return INDEX.read_bytes().decode('utf-8-sig').replace('\r\n', '\n')


@pytest.fixture
def client():
    with client_for(api_dashboard_path=str(INDEX)) as c:
        yield c


def write(tmp_path, html, name='page.html'):
    path = tmp_path / name
    path.write_bytes(html.encode() if isinstance(html, str) else html)
    return path


# ---------------------------------------------------------------- serving
def test_the_page_is_served_at_the_root(client):
    r = client.get('/')
    assert r.status_code == 200
    assert r.headers['content-type'] == 'text/html; charset=utf-8'
    assert r.text == page_text()
    assert r.headers['cache-control'] == 'no-cache'
    assert r.headers['etag'].startswith('W/"')
    assert r.headers['x-request-id']
    assert 'x-cache' not in r.headers            # the response cache is not involved


def test_the_page_has_the_security_headers(client):
    h = client.get('/').headers
    assert h['x-content-type-options'] == 'nosniff'
    assert h['x-frame-options'] == 'DENY'
    assert h['referrer-policy'] == 'no-referrer'
    assert "frame-ancestors 'none'" in h['content-security-policy']


def test_conditional_request_gets_304_with_the_policy(client):
    first = client.get('/')
    r = client.get('/', headers={'If-None-Match': first.headers['etag']})
    assert r.status_code == 304 and r.content == b''
    assert r.headers['etag'] == first.headers['etag']
    assert r.headers['content-security-policy'] == first.headers['content-security-policy']
    assert client.get('/', headers={'If-None-Match': 'W/"other"'}).status_code == 200


def test_head_has_no_body(client):
    r = client.head('/')
    assert r.status_code == 200 and r.content == b''
    assert 'content-security-policy' in r.headers and r.headers['etag']


def test_other_paths_and_methods_are_problems(client):
    assert_problem(client.get('/index.html'), 404, 'not-found')
    assert_problem(client.get('/static/x.js'), 404, 'not-found')
    assert_problem(client.post('/'), 405, 'method-not-allowed')


def test_no_page_unless_configured():
    with client_for() as c:
        assert_problem(c.get('/'), 404, 'not-found')


def test_the_page_is_public_but_the_data_is_not():
    with client_for(api_dashboard_path=str(INDEX), api_auth_mode='api_key',
                    api_keys=['k' * 20]) as c:
        assert c.get('/').status_code == 200
        assert_problem(c.get('/api/overview'), 401, 'unauthorized')
        assert c.get('/api/health').status_code == 200


# ---------------------------------------------------------------- the policy
def test_csp_never_allows_unsafe_inline_or_eval(client):
    csp = client.get('/').headers['content-security-policy']
    assert 'unsafe-inline' not in csp and 'unsafe-eval' not in csp
    assert "'unsafe-hashes'" in directive(csp, 'style-src-attr')   # hash-pinned, attributes only
    assert directive(csp, 'default-src') == ["'none'"]
    assert directive(csp, 'connect-src') == ["'self'"]
    assert directive(csp, 'base-uri') == ["'none'"] and directive(csp, 'form-action') == ["'none'"]
    assert directive(csp, 'frame-ancestors') == ["'none'"]


def test_script_src_is_the_hash_of_the_inline_script_and_the_exact_chart_url(client):
    csp = client.get('/').headers['content-security-policy']
    inline = re.findall(r'<script>(.*?)</script>', page_text(), re.S)
    assert len(inline) == 1
    assert directive(csp, 'script-src') == [sha(inline[0]), CHART_JS]


def test_style_policy_covers_the_markup_exactly(client):
    csp = client.get('/').headers['content-security-policy']
    text = page_text()
    blocks = re.findall(r'<style>(.*?)</style>', text, re.S)
    assert directive(csp, 'style-src') == [sha(b) for b in blocks] + ['https://fonts.googleapis.com']
    attrs = {a for a in re.findall(r'\sstyle="([^"]*)"', text)}
    assert directive(csp, 'style-src-attr') == ["'unsafe-hashes'"] + [sha(a) for a in dict.fromkeys(
        a for a in re.findall(r'\sstyle="([^"]*)"', text))]
    assert len(attrs) >= 1
    assert directive(csp, 'font-src') == ['https://fonts.gstatic.com']


def test_the_page_needs_no_inline_handlers_or_inline_style_in_templates():
    text = page_text()
    assert not re.search(r'\son[a-z]+\s*=', text)              # addEventListener only
    assert not re.search(r'javascript:', text, re.I)
    script = re.findall(r'<script>(.*?)</script>', text, re.S)[0]
    assert not re.search(r'(?<![\w-])style\s*=\s*"', script)   # generated markup uses data-style


def test_crlf_checkout_gives_the_same_page_and_policy(tmp_path):
    lf = load_dashboard(INDEX)
    crlf = load_dashboard(write(tmp_path, page_text().replace('\n', '\r\n'), 'crlf.html'))
    assert crlf.body == lf.body and crlf.csp == lf.csp and crlf.etag == lf.etag


def test_changing_the_page_changes_its_hash(tmp_path):
    original = load_dashboard(INDEX)
    changed = load_dashboard(write(tmp_path, page_text().replace('applyStyles(root)', 'applyStyles(r)', 1)))
    assert changed.csp != original.csp and changed.etag != original.etag


def test_a_page_without_scripts_or_styles_gets_none(tmp_path):
    csp = load_dashboard(write(tmp_path, '<!doctype html><title>x</title><p>hi</p>')).csp
    for name in ('script-src', 'style-src', 'style-src-attr', 'font-src'):
        assert directive(csp, name) == ["'none'"]


# ---------------------------------------------------------------- startup refusals
@pytest.mark.parametrize('html, message', [
    ('<button onclick="go()">x</button>', 'inline event handlers'),
    ('<input onkeydown="x()">', 'inline event handlers'),
    ('<a href="javascript:alert(1)">x</a>', 'javascript:'),
    ('<script src="https://evil.example/x.js"></script>', 'allowed host'),
    ('<script src="http://cdnjs.cloudflare.com/x.js"></script>', 'https URL'),
    ('<script src="https://cdnjs.cloudflare.com/x.js?v=1"></script>', 'query string'),
    ('<link rel="stylesheet" href="https://evil.example/x.css">', 'allowed host'),
])
def test_unsafe_pages_are_refused(tmp_path, html, message):
    with pytest.raises(DashboardError, match=message):
        load_dashboard(write(tmp_path, html))


def test_missing_oversized_and_binary_files_are_refused(tmp_path):
    with pytest.raises(DashboardError, match='is not a file'):
        load_dashboard(tmp_path / 'nope.html')
    with pytest.raises(DashboardError, match='larger than'):
        load_dashboard(write(tmp_path, 'x' * (2 * 1024 * 1024 + 1)))
    with pytest.raises(DashboardError, match='UTF-8'):
        load_dashboard(write(tmp_path, b'\xff\xfe\x00bad'))


def test_the_app_refuses_to_start_on_a_bad_page(tmp_path):
    with pytest.raises(DashboardError):
        create_app(make_settings(api_dashboard_path=str(write(tmp_path, '<b onclick="x()">'))))


def test_errors_never_echo_the_file_content(tmp_path):
    secret = 'SECRET-TOKEN-1234'
    with pytest.raises(DashboardError) as exc:
        load_dashboard(write(tmp_path, f'<p>{secret}</p><button onclick="x()">'))
    assert secret not in str(exc.value)


def test_cli_exits_with_status_2_and_names_the_variable(tmp_path, monkeypatch, capsys):
    from api.__main__ import main
    monkeypatch.setenv('API_DB_PASSWORD', FAKE_PASSWORD)
    monkeypatch.setenv('API_DASHBOARD_PATH', str(tmp_path / 'missing.html'))
    assert main() == 2
    assert 'API_DASHBOARD_PATH' in capsys.readouterr().err


def test_blank_dashboard_path_means_unset():
    assert make_settings(api_dashboard_path='').api_dashboard_path is None
    assert make_settings(api_dashboard_path='  ').api_dashboard_path is None


# ---------------------------------------------------------------- the API is untouched
def test_the_api_keeps_its_strict_policy_and_the_page_route_is_not_in_the_contract(client):
    r = client.get('/api/health')
    assert r.headers['content-security-policy'] == "default-src 'none'; frame-ancestors 'none'"
    assert '/' not in client.app.openapi()['paths']
    assert all(p.startswith('/api/') for p in client.app.openapi()['paths'])


def test_the_settings_summary_still_masks_secrets():
    summary = make_settings(api_dashboard_path=str(INDEX)).summary()
    assert summary['api_db_password'] == '***'
    assert summary['api_dashboard_path'] == str(INDEX)
