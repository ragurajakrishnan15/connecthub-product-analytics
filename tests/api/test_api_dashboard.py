"""The dashboard page served at GET / (PHASE_5_PLAN.md step 1): serving, the derived
Content-Security-Policy, startup refusals, and that the API contract is untouched.
No database is needed."""
import base64
import hashlib
import re
import shutil
from pathlib import Path

import pytest
from api_testlib import (FAKE_PASSWORD, assert_problem, clean_env,  # noqa: F401
                         client_for, make_settings)

from api.dashboard import DashboardError, load_dashboard
from api.main import create_app

pytestmark = pytest.mark.usefixtures('clean_env')

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / 'index.html'
VENDORED = ROOT / 'vendor' / 'chart.umd.js'
CHART_URL_PATH = '/vendor/chart.umd.js'
# What the vendored file is (vendor/README.md): npm chart.js@4.4.1 dist/chart.umd.js without its
# final "//# sourceMappingURL=..." line.
NPM_ORIGINAL_SHA256 = '74401d738dd3e03ee5dfb3b6841210fe2c4ead8a960c4011ca4ba0b78a9fd8f3'
VENDORED_SHA256 = '0ee28337f25838a7a5d6b1e8b2b02279ab10e80e17c217a99893a7a717f2ba05'
VENDORED_SIZE = 205087
SOURCE_MAP_LINE = b'//# sourceMappingURL=chart.umd.js.map\n'


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
    assert 'unsafe-hashes' not in csp                               # the page has no style attributes at all
    assert directive(csp, 'style-src-attr') == ["'none'"]
    assert directive(csp, 'default-src') == ["'none'"]
    assert directive(csp, 'connect-src') == ["'self'"]
    assert directive(csp, 'base-uri') == ["'none'"] and directive(csp, 'form-action') == ["'none'"]
    assert directive(csp, 'frame-ancestors') == ["'none'"]


def test_script_src_is_the_hash_of_the_inline_script_and_this_origin_only(client):
    csp = client.get('/').headers['content-security-policy']
    inline = re.findall(r'<script>(.*?)</script>', page_text(), re.S)
    assert len(inline) == 1
    assert directive(csp, 'script-src') == [sha(inline[0]), "'self'"]


def test_style_policy_covers_the_markup_exactly(client):
    csp = client.get('/').headers['content-security-policy']
    text = page_text()
    blocks = re.findall(r'<style>(.*?)</style>', text, re.S)
    assert directive(csp, 'style-src') == [sha(b) for b in blocks] + ['https://fonts.googleapis.com']
    assert not re.findall(r'\sstyle\s*=', text)                     # layout uses classes; styles set from script use the DOM
    assert directive(csp, 'style-src-attr') == ["'none'"]
    assert directive(csp, 'font-src') == ['https://fonts.gstatic.com']


def test_the_page_needs_no_inline_handlers_or_inline_style_in_templates():
    text = page_text()
    assert not re.search(r'\son[a-z]+\s*=', text)              # addEventListener only
    assert not re.search(r'javascript:', text, re.I)
    script = re.findall(r'<script>(.*?)</script>', text, re.S)[0]
    assert not re.search(r'(?<![\w-])style\s*=\s*"', script)   # generated markup sets styles through the DOM


def with_vendor(tmp_path):
    """A temp directory holding a copy of the vendored files, so a copy of the page can find its script."""
    shutil.copytree(ROOT / 'vendor', tmp_path / 'vendor')
    return tmp_path


def test_crlf_checkout_gives_the_same_page_and_policy(tmp_path):
    lf = load_dashboard(INDEX)
    crlf = load_dashboard(write(with_vendor(tmp_path), page_text().replace('\n', '\r\n'), 'crlf.html'))
    assert crlf.body == lf.body and crlf.csp == lf.csp and crlf.etag == lf.etag
    assert crlf.assets[0].body == lf.assets[0].body            # scripts are never rewritten


def test_changing_the_page_changes_its_hash(tmp_path):
    original = load_dashboard(INDEX)
    edited = page_text().replace('const EM_DASH', 'const EM_DASH_', 1)
    assert edited != page_text()
    changed = load_dashboard(write(with_vendor(tmp_path), edited))
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
    ('<script src="https://evil.example/x.js"></script>', 'is remote'),
    ('<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>', 'is remote'),
    ('<script src="http://cdnjs.cloudflare.com/x.js"></script>', 'is remote'),
    ('<script src="//cdnjs.cloudflare.com/x.js"></script>', 'is remote'),
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


# ---------------------------------------------------------------- Chart.js is served from this origin
def sri384(data):
    return 'sha384-' + base64.b64encode(hashlib.sha384(data).digest()).decode()


def test_chart_js_is_served_from_the_same_origin(client):
    r = client.get(CHART_URL_PATH)
    assert r.status_code == 200
    assert r.headers['content-type'] == 'text/javascript; charset=utf-8'
    assert r.content == VENDORED.read_bytes()
    assert r.headers['etag'].startswith('W/"') and r.headers['cache-control'] == 'no-cache'
    assert r.headers['x-content-type-options'] == 'nosniff' and r.headers['x-request-id']
    assert 'x-cache' not in r.headers                         # the response cache is not involved


def test_the_page_loads_it_from_a_relative_path_and_names_no_cdn():
    text = page_text()
    tags = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"[^>]*>', text)
    assert tags == ['vendor/chart.umd.js']
    assert 'cdnjs' not in text and 'cloudflare' not in text.lower()
    assert not re.search(r'<script[^>]*\bsrc="(?:https?:)?//', text)


def test_the_integrity_attribute_is_the_hash_of_the_vendored_file():
    tag = re.search(r'<script\b[^>]*\bsrc="vendor/chart\.umd\.js"[^>]*>', page_text()).group(0)
    assert re.search(r'\bintegrity="%s"' % re.escape(sri384(VENDORED.read_bytes())), tag)


def test_the_csp_no_longer_permits_the_cdn_or_any_remote_script_host(client):
    csp = client.get('/').headers['content-security-policy']
    scripts = directive(csp, 'script-src')
    assert 'cdnjs' not in csp and 'cloudflare.com' not in csp.replace('fonts.googleapis.com', '')
    assert not [s for s in scripts if s.startswith(('http:', 'https:', '*', '//'))]
    assert "'self'" in scripts and sum(s.startswith("'sha256-") for s in scripts) == 1
    for banned in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "'strict-dynamic'", '*', 'data:', 'blob:'):
        assert banned not in scripts
    assert directive(csp, 'connect-src') == ["'self'"]


def test_the_asset_supports_conditional_requests_and_head(client):
    first = client.get(CHART_URL_PATH)
    r = client.get(CHART_URL_PATH, headers={'If-None-Match': first.headers['etag']})
    assert r.status_code == 304 and r.content == b'' and r.headers['etag'] == first.headers['etag']
    assert client.get(CHART_URL_PATH, headers={'If-None-Match': 'W/"other"'}).status_code == 200
    h = client.head(CHART_URL_PATH)
    assert h.status_code == 200 and h.content == b'' and h.headers['etag'] == first.headers['etag']


def test_only_the_named_files_are_served(client):
    for path in ('/vendor/', '/vendor', '/vendor/other.js', '/vendor/chart.umd.js.map', '/vendor/LICENSE-chartjs.md',
                 '/vendor/README.md', '/vendor/%2e%2e/index.html', '/vendor/../index.html', '/index.html',
                 '/vendor/chart.umd.JS'):
        assert client.get(path).status_code in (404, 405), path
    assert_problem(client.get('/vendor/other.js'), 404, 'not-found')
    assert_problem(client.post(CHART_URL_PATH), 405, 'method-not-allowed')


def test_the_script_is_public_so_the_page_can_load_with_auth_on():
    with client_for(api_dashboard_path=str(INDEX), api_auth_mode='api_key', api_keys=['k' * 20]) as c:
        assert c.get(CHART_URL_PATH).status_code == 200
        assert_problem(c.get('/api/overview'), 401, 'unauthorized')


def test_the_asset_route_is_not_in_the_api_contract(client):
    paths = client.app.openapi()['paths']
    assert CHART_URL_PATH not in paths and all(p.startswith('/api/') for p in paths)


def test_the_api_responses_still_carry_the_strict_policy_and_no_script_source(client):
    csp = client.get('/api/health').headers['content-security-policy']
    assert csp == "default-src 'none'; frame-ancestors 'none'"


def test_a_page_without_local_scripts_does_not_get_self(tmp_path):
    csp = load_dashboard(write(tmp_path, '<!doctype html><script>var a=1</script>')).csp
    assert "'self'" not in directive(csp, 'script-src')


def test_the_loader_reports_the_asset_it_serves():
    d = load_dashboard(INDEX)
    assert [a.url_path for a in d.assets] == [CHART_URL_PATH]
    assert d.assets[0].body == VENDORED.read_bytes()
    assert d.summary()['assets'] == [CHART_URL_PATH]


# ---------------------------------------------------------------- the vendored file is the expected 4.4.1 artifact
def test_the_vendored_file_is_the_expected_size_and_hash():
    data = VENDORED.read_bytes()
    assert len(data) == VENDORED_SIZE
    assert hashlib.sha256(data).hexdigest() == VENDORED_SHA256


def test_the_vendored_file_plus_the_removed_line_is_exactly_the_npm_artifact():
    original = VENDORED.read_bytes() + SOURCE_MAP_LINE
    assert len(original) == 205125
    assert hashlib.sha256(original).hexdigest() == NPM_ORIGINAL_SHA256


def test_the_vendored_file_is_chart_js_4_4_1_with_its_mit_banner_and_no_source_map():
    data = VENDORED.read_bytes()
    head = data[:200].decode()
    assert head.startswith('/*!\n * Chart.js v4.4.1\n')
    assert '(c) 2023 Chart.js Contributors' in head and 'Released under the MIT License' in head
    assert b'static version="4.4.1"' in data
    assert b'sourceMappingURL' not in data
    assert data.endswith(b'}));\n')


def test_the_vendored_bytes_are_unconverted():
    data = VENDORED.read_bytes()
    assert b'\r' not in data and not data.startswith(b'\xef\xbb\xbf')
    assert all(b < 128 for b in data)


def test_git_will_not_convert_the_vendored_files():
    rules = (ROOT / '.gitattributes').read_text().splitlines()
    assert 'vendor/** -text' in [r.strip() for r in rules]


def test_the_mit_license_text_ships_with_it():
    lic = (ROOT / 'vendor' / 'LICENSE-chartjs.md').read_text()
    assert lic.startswith('The MIT License (MIT)') and 'Chart.js Contributors' in lic
    assert 'Permission is hereby granted' in lic
    assert hashlib.sha256((ROOT / 'vendor' / 'LICENSE-chartjs.md').read_bytes()).hexdigest() == \
        '5a0877ad6d818529be4f33009d0942cdf7e2ed7656156f4aba7308459a546030'


def test_the_provenance_note_states_the_same_hashes():
    readme = (ROOT / 'vendor' / 'README.md').read_text()
    for needle in (NPM_ORIGINAL_SHA256, VENDORED_SHA256, sri384(VENDORED.read_bytes()), '4.4.1'):
        assert needle in readme


def test_the_docker_image_copies_the_vendored_files():
    dockerfile = (ROOT / 'docker' / 'api' / 'Dockerfile').read_text()
    assert 'COPY vendor/ dashboard/vendor/' in dockerfile
    ignored = [ln.strip() for ln in (ROOT / '.dockerignore').read_text().splitlines()]
    assert not any(ln.rstrip('/').startswith('vendor') or ln in ('*', '**') for ln in ignored)


# ---------------------------------------------------------------- local scripts are checked at startup
def page_with(tmp_path, tag, files=None):
    for name, data in (files or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return write(tmp_path, f'<!doctype html>{tag}')


SCRIPT = b'window.x = 1;\n'


def test_a_matching_local_script_is_served_and_allowed(tmp_path):
    page = page_with(tmp_path, f'<script src="lib/a.js" integrity="{sri384(SCRIPT)}"></script>', {'lib/a.js': SCRIPT})
    d = load_dashboard(page)
    assert [(a.url_path, a.body) for a in d.assets] == [('/lib/a.js', SCRIPT)]
    assert directive(d.csp, 'script-src') == ["'self'"]


@pytest.mark.parametrize('algo, digest', [('sha256', hashlib.sha256), ('sha512', hashlib.sha512)])
def test_other_integrity_algorithms_are_accepted(tmp_path, algo, digest):
    sri = f'{algo}-' + base64.b64encode(digest(SCRIPT).digest()).decode()
    load_dashboard(page_with(tmp_path, f'<script src="a.js" integrity="{sri}"></script>', {'a.js': SCRIPT}))


@pytest.mark.parametrize('integrity, message', [
    ('', 'needs an integrity attribute'),
    ('md5-abc', 'needs an integrity attribute'),
    ('sha384-' + 'A' * 64, 'does not match'),
])
def test_a_local_script_must_carry_a_matching_integrity(tmp_path, integrity, message):
    page = page_with(tmp_path, f'<script src="a.js" integrity="{integrity}"></script>', {'a.js': SCRIPT})
    with pytest.raises(DashboardError, match=message):
        load_dashboard(page)


def test_every_listed_integrity_token_must_match(tmp_path):
    good = sri384(SCRIPT)
    page = page_with(tmp_path, f'<script src="a.js" integrity="{good} sha256-AAAA"></script>', {'a.js': SCRIPT})
    with pytest.raises(DashboardError, match='does not match'):
        load_dashboard(page)


@pytest.mark.parametrize('src, message', [
    ('../a.js', 'simple relative path'),
    ('a/../a.js', 'simple relative path'),
    ('/etc/a.js', 'plain relative path'),
    ('a.js?v=1', 'plain relative path'),
    ('a.js#x', 'plain relative path'),
    ('%2e%2e/a.js', 'plain relative path'),
    ('a\\b.js', 'plain relative path'),
    ('a.css', 'simple relative path'),
    ('a.mjs', 'simple relative path'),
    ('.hidden.js', 'simple relative path'),
    ('a b.js', 'simple relative path'),
    ('file:///a.js', 'is remote'),
    ('data:text/javascript,1', 'is remote'),
])
def test_unsafe_local_script_paths_are_refused(tmp_path, src, message):
    page = page_with(tmp_path, f'<script src="{src}" integrity="{sri384(SCRIPT)}"></script>', {'a.js': SCRIPT})
    with pytest.raises(DashboardError, match=message):
        load_dashboard(page)


def test_a_missing_or_oversized_script_is_refused(tmp_path):
    with pytest.raises(DashboardError, match='was not found'):
        load_dashboard(page_with(tmp_path, f'<script src="nope.js" integrity="{sri384(SCRIPT)}"></script>'))
    big = b'x' * (2 * 1024 * 1024 + 1)
    with pytest.raises(DashboardError, match='larger than'):
        load_dashboard(page_with(tmp_path, f'<script src="big.js" integrity="{sri384(big)}"></script>', {'big.js': big}))


def test_a_script_that_resolves_outside_the_page_directory_is_refused(tmp_path):
    site = tmp_path / 'site'
    site.mkdir()
    (tmp_path / 'secret.js').write_bytes(SCRIPT)
    link = site / 'link.js'
    try:
        link.symlink_to(tmp_path / 'secret.js')
    except (OSError, NotImplementedError):
        pytest.skip('symlinks are not available here')
    page = write(site, f'<!doctype html><script src="link.js" integrity="{sri384(SCRIPT)}"></script>')
    with pytest.raises(DashboardError, match='outside the dashboard directory'):
        load_dashboard(page)


def test_script_errors_never_echo_the_file_content(tmp_path):
    secret = b'SECRET-TOKEN-1234'
    page = page_with(tmp_path, '<script src="a.js" integrity="sha384-AAAA"></script>', {'a.js': secret})
    with pytest.raises(DashboardError) as exc:
        load_dashboard(page)
    assert 'SECRET-TOKEN-1234' not in str(exc.value)


def test_a_page_with_a_bad_script_does_not_start_the_app(tmp_path):
    page = page_with(tmp_path, '<script src="https://cdnjs.cloudflare.com/x.js"></script>')
    with pytest.raises(DashboardError, match='is remote'):
        create_app(make_settings(api_dashboard_path=str(page)))
