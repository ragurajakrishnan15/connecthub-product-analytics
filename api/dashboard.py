"""
Serves the single-file dashboard (index.html) at GET / when API_DASHBOARD_PATH is set
(PHASE_5_PLAN.md section 6), and the vendored scripts it loads from its own directory.

The page is read once at startup, and its Content-Security-Policy is derived from its
own content, so the policy always matches the file that is served:

- script-src:      the SHA-256 of every inline <script>, plus 'self' when the page loads
                   a script from this origin (see "Local scripts"). No 'unsafe-inline',
                   no 'unsafe-eval', and no remote script host at all.
- style-src:       the SHA-256 of every inline <style>, plus the stylesheet hosts.
- style-src-attr:  'unsafe-hashes' with the SHA-256 of each distinct style="" value in the
                   markup (none today: 'none'). Styles set from script (element.style.*)
                   are not restricted by CSP.
- connect-src:     'self' (the page calls this API on its own origin).

Local scripts: a <script src="vendor/x.js" integrity="sha384-..."> next to the page is read at
startup and served from the same path, byte for byte. Startup fails unless the src is a plain
relative path that stays inside the page's directory (no scheme, query, fragment, "..",
percent-encoding or symlink out), the file is a .js file under MAX_BYTES, and the integrity
attribute matches the file. Only those exact files are served: any other path is a 404.

Startup also fails if the page has inline event handlers (onclick=...), javascript: URLs,
a remote script, or a stylesheet outside the allow-list, because the policy would block them.
Line endings of the page are normalized to LF before hashing and serving: browsers do the same
before hashing, so Windows and Linux checkouts give the same policy. Scripts are never
rewritten (a vendored file's bytes must match its integrity hash).

The routes are outside /api, are not in the OpenAPI schema, are public (the page holds no
data; the data endpoints enforce API_AUTH_MODE themselves) and are never cached by
CacheMiddleware.
"""
import base64
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import Response

from api.cache import etag_matches

MAX_BYTES = 2 * 1024 * 1024
# Remote hosts the page may load from. No script host is allowed: scripts are vendored
# (vendor/README.md). Google Fonts serves the page's typefaces.
STYLESHEET_HOSTS = frozenset({'fonts.googleapis.com'})
FONT_HOSTS = ('https://fonts.gstatic.com',)
_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_DIGESTS = {'sha256': hashlib.sha256, 'sha384': hashlib.sha384, 'sha512': hashlib.sha512}


class DashboardError(ValueError):
    """The dashboard file cannot be served safely. Messages never contain file content."""


@dataclass(frozen=True)
class Asset:
    url_path: str          # e.g. "/vendor/chart.umd.js"
    body: bytes
    etag: str
    media_type: str


@dataclass(frozen=True)
class Dashboard:
    path: Path
    body: bytes
    etag: str
    csp: str
    inline_scripts: int
    inline_styles: int
    style_attributes: int
    assets: tuple = ()

    def summary(self):
        return {'path': str(self.path), 'bytes': len(self.body), 'inline_scripts': self.inline_scripts,
                'inline_styles': self.inline_styles, 'style_attributes': self.style_attributes,
                'assets': [a.url_path for a in self.assets]}


def _sha256(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode() + "'"


class _Inventory(HTMLParser):
    """Everything in the page that a Content-Security-Policy has to account for."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.styles = [], []
        self.script_srcs, self.stylesheets = [], []     # script_srcs: (src, integrity)
        self.style_attrs = []
        self.handlers = []
        self.js_urls = []
        self._open = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or '') for k, v in attrs}
        for name, value in attrs.items():
            if name.startswith('on') and len(name) > 2:
                self.handlers.append(f'<{tag} {name}>')
            if name == 'style':
                self.style_attrs.append(value)
            if name in ('href', 'src', 'action', 'formaction') and \
                    value.strip().lower().startswith('javascript:'):
                self.js_urls.append(f'<{tag} {name}>')
        if tag == 'script':
            if attrs.get('src'):
                self.script_srcs.append((attrs['src'], attrs.get('integrity', '')))
            else:
                self._open, self._text = 'script', []
        elif tag == 'style':
            self._open, self._text = 'style', []
        elif tag == 'link' and 'stylesheet' in attrs.get('rel', '').lower().split():
            self.stylesheets.append(attrs.get('href', ''))

    def handle_data(self, data):
        if self._open:
            self._text.append(data)

    def handle_endtag(self, tag):
        if self._open == tag:
            (self.scripts if tag == 'script' else self.styles).append(''.join(self._text))
            self._open = None


def _https_host(url, allowed, what):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in allowed or parts.username or parts.port:
        raise DashboardError(f'{what} {url!r} is not an https URL on an allowed host '
                             f'({", ".join(sorted(allowed))})')
    return parts


def _local_segments(src):
    """The path segments of a plain relative script path, or DashboardError."""
    parts = urlsplit(src)
    if parts.scheme or parts.netloc:
        raise DashboardError(f'script {src!r} is remote; remote scripts are not allowed '
                             '(vendor it next to the page, see vendor/README.md)')
    if parts.query or parts.fragment or '%' in src or '\\' in src or src.startswith('/'):
        raise DashboardError(f'script {src!r} must be a plain relative path '
                             '(no leading slash, query, fragment, backslash or percent-encoding)')
    segments = src.split('/')
    if not all(_SEGMENT.match(s) for s in segments) or not segments[-1].endswith('.js'):
        raise DashboardError(f'script {src!r} must be a .js file with a simple relative path')
    return segments


def _check_integrity(src, integrity, body):
    tokens = integrity.split()
    supported = [t for t in tokens if t.split('-', 1)[0] in _DIGESTS and '-' in t]
    if not supported:
        raise DashboardError(f'script {src!r} needs an integrity attribute (sha256-, sha384- or sha512-)')
    for token in supported:
        algo, expected = token.split('-', 1)
        actual = base64.b64encode(_DIGESTS[algo](body).digest()).decode()
        if actual != expected:
            raise DashboardError(f'script {src!r} does not match its integrity attribute ({algo})')


def _load_asset(page_dir, src, integrity):
    segments = _local_segments(src)
    path = page_dir.joinpath(*segments).resolve()
    if not path.is_relative_to(page_dir):
        raise DashboardError(f'script {src!r} is outside the dashboard directory')
    if not path.is_file():
        raise DashboardError(f'script {src!r} was not found next to the page')
    if path.stat().st_size > MAX_BYTES:
        raise DashboardError(f'script {src!r} is larger than {MAX_BYTES} bytes')
    body = path.read_bytes()
    _check_integrity(src, integrity, body)
    return Asset(url_path='/' + '/'.join(segments), body=body,
                 etag='W/"' + hashlib.sha256(body).hexdigest()[:32] + '"',
                 media_type='text/javascript; charset=utf-8')


def build_csp(inv, local_scripts=False):
    script = [_sha256(t) for t in dict.fromkeys(inv.scripts)]
    if local_scripts:
        script.append("'self'")                 # only the files the page names are served
    style = [_sha256(t) for t in dict.fromkeys(inv.styles)]
    fonts = False
    for url in dict.fromkeys(inv.stylesheets):
        parts = _https_host(url, STYLESHEET_HOSTS, 'stylesheet')
        style.append(f'https://{parts.hostname}')
        fonts = True
    attrs = [_sha256(v) for v in dict.fromkeys(inv.style_attrs)]
    directives = [
        "default-src 'none'",
        'script-src ' + (' '.join(script) or "'none'"),
        'style-src ' + (' '.join(style) or "'none'"),
        'style-src-attr ' + (("'unsafe-hashes' " + ' '.join(attrs)) if attrs else "'none'"),
        'font-src ' + (' '.join(FONT_HOSTS) if fonts else "'none'"),
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
    return '; '.join(directives)


def load_dashboard(path):
    """Read, check and fingerprint the dashboard file and its local scripts. Raises DashboardError."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise DashboardError(f'API_DASHBOARD_PATH {str(path)!r} is not a file')
    if path.stat().st_size > MAX_BYTES:
        raise DashboardError(f'dashboard file is larger than {MAX_BYTES} bytes')
    try:
        text = path.read_bytes().decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise DashboardError('dashboard file is not valid UTF-8') from exc
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    inv = _Inventory()
    inv.feed(text)
    inv.close()
    if inv.handlers:
        raise DashboardError('inline event handlers are blocked by the Content-Security-Policy; '
                             'use addEventListener instead: ' + ', '.join(sorted(set(inv.handlers))))
    if inv.js_urls:
        raise DashboardError('javascript: URLs are not allowed: ' + ', '.join(sorted(set(inv.js_urls))))
    assets = {}
    for src, integrity in inv.script_srcs:
        asset = _load_asset(path.parent, src, integrity)
        assets[asset.url_path] = asset
    csp = build_csp(inv, local_scripts=bool(assets))
    body = text.encode('utf-8')
    return Dashboard(path=path, body=body, etag='W/"' + hashlib.sha256(body).hexdigest()[:32] + '"',
                     csp=csp, inline_scripts=len(set(inv.scripts)), inline_styles=len(set(inv.styles)),
                     style_attributes=len(set(inv.style_attrs)), assets=tuple(assets.values()))


def _asset_endpoint(asset):
    headers = {'ETag': asset.etag, 'Cache-Control': 'no-cache'}

    async def endpoint(request: Request):
        if etag_matches(request.headers.get('if-none-match'), asset.etag):
            return Response(status_code=304, headers=headers)
        return Response(asset.body, media_type=asset.media_type, headers=headers)
    return endpoint


def dashboard_router(dashboard):
    router = APIRouter(include_in_schema=False)
    headers = {'ETag': dashboard.etag, 'Cache-Control': 'no-cache',
               'Content-Security-Policy': dashboard.csp}

    @router.get('/')
    async def index(request: Request):
        if etag_matches(request.headers.get('if-none-match'), dashboard.etag):
            return Response(status_code=304, headers=headers)
        return Response(dashboard.body, media_type='text/html; charset=utf-8', headers=headers)

    for asset in dashboard.assets:
        router.add_api_route(asset.url_path, _asset_endpoint(asset), methods=['GET'],
                             include_in_schema=False)
    return router
