"""
Serves the single-file dashboard (index.html) at GET / when API_DASHBOARD_PATH is set
(PHASE_5_PLAN.md section 6).

The page is read once at startup, and its Content-Security-Policy is derived from its
own content, so the policy always matches the file that is served:

- script-src:      the SHA-256 of every inline <script>, plus the exact URL of each
                   external script (an https URL on an allow-listed host). No 'unsafe-inline'.
- style-src:       the SHA-256 of every inline <style>, plus the stylesheet hosts.
- style-src-attr:  'unsafe-hashes' with the SHA-256 of each distinct style="" value in the
                   markup. Styles set from script (element.style.*) are not restricted by CSP.
- connect-src:     'self' (the page calls this API on its own origin).

Startup fails if the page has inline event handlers (onclick=...), javascript: URLs or an
external script/stylesheet outside the allow-list, because the policy would block them.
Line endings are normalized to LF before hashing and serving: browsers do the same
before hashing, so Windows and Linux checkouts give the same policy.

The route is outside /api, is not in the OpenAPI schema, is public (the page holds no
data; the data endpoints enforce API_AUTH_MODE themselves) and is never cached by
CacheMiddleware.
"""
import base64
import hashlib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import Response

from api.cache import etag_matches

MAX_BYTES = 2 * 1024 * 1024
# Hosts the page may load from. cdnjs hosts Chart.js until it is vendored (PHASE_5_PLAN.md
# step 9, which removes it); Google Fonts serves the page's typefaces.
SCRIPT_HOSTS = frozenset({'cdnjs.cloudflare.com'})
STYLESHEET_HOSTS = frozenset({'fonts.googleapis.com'})
FONT_HOSTS = ('https://fonts.gstatic.com',)


class DashboardError(ValueError):
    """The dashboard file cannot be served safely. Messages never contain file content."""


@dataclass(frozen=True)
class Dashboard:
    path: Path
    body: bytes
    etag: str
    csp: str
    inline_scripts: int
    inline_styles: int
    style_attributes: int

    def summary(self):
        return {'path': str(self.path), 'bytes': len(self.body), 'inline_scripts': self.inline_scripts,
                'inline_styles': self.inline_styles, 'style_attributes': self.style_attributes}


def _sha256(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode() + "'"


class _Inventory(HTMLParser):
    """Everything in the page that a Content-Security-Policy has to account for."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.styles = [], []
        self.external_scripts, self.stylesheets = [], []
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
                self.external_scripts.append(attrs['src'])
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


def build_csp(inv):
    script = [_sha256(t) for t in dict.fromkeys(inv.scripts)]
    for url in dict.fromkeys(inv.external_scripts):
        parts = _https_host(url, SCRIPT_HOSTS, 'script')
        if parts.query or parts.fragment:
            raise DashboardError(f'script {url!r} must not have a query string or fragment')
        script.append(url)                      # the exact URL, not the whole host
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
    """Read, check and fingerprint the dashboard file. Raises DashboardError."""
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
    csp = build_csp(inv)
    body = text.encode('utf-8')
    return Dashboard(path=path, body=body, etag='W/"' + hashlib.sha256(body).hexdigest()[:32] + '"',
                     csp=csp, inline_scripts=len(set(inv.scripts)), inline_styles=len(set(inv.styles)),
                     style_attributes=len(set(inv.style_attrs)))


def dashboard_router(dashboard):
    router = APIRouter(include_in_schema=False)
    headers = {'ETag': dashboard.etag, 'Cache-Control': 'no-cache',
               'Content-Security-Policy': dashboard.csp}

    @router.get('/')
    async def index(request: Request):
        if etag_matches(request.headers.get('if-none-match'), dashboard.etag):
            return Response(status_code=304, headers=headers)
        return Response(dashboard.body, media_type='text/html; charset=utf-8', headers=headers)

    return router
