"""
Response cache and HTTP validators for the data endpoints (PHASE_4_PLAN.md §8.2).

- What is cached: successful (200, JSON) GET/HEAD responses of /api/meta and
  the business endpoints. Never errors, never /api/health or /api/health/ready,
  never docs.
- Key: (path, sorted query parameters, data_version). data_version is the
  run_id of the last fully validated pipeline run (meta.data_version), looked
  up at most every API_DATA_VERSION_TTL_S seconds. When a new run validates,
  the key changes and every old entry is unreachable (and ages out). Without a
  validated run (data_version null) nothing is cached.
- Bounds: entries expire after API_CACHE_TTL_S; at most API_CACHE_MAX_ENTRIES
  entries and API_CACHE_MAX_BYTES bytes per process, least recently used first.
- Per process: each uvicorn worker has its own cache. ETags are computed from
  the response content (excluding meta.generated_at), so all workers agree.
- Authentication runs before the cache: with API_AUTH_MODE=api_key an entry is
  only served to a request with a valid key (otherwise the route answers 401).

ETag: W/"<sha256 of the canonical JSON body without meta.generated_at>"[:32].
A weak validator: the body is semantically identical when the ETag matches
(only the generation timestamp may differ, and GZip may re-encode it).
If-None-Match matching the ETag -> 304 Not Modified with no body.
"""
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qsl

import anyio.to_thread
from starlette.datastructures import Headers

from api.context import request_state_var
from api.repositories import system
from api.security import api_key_valid
from pipeline.log import redact

log = logging.getLogger('connecthub.api')

CACHEABLE_PATHS = frozenset({
    '/api/meta', '/api/overview', '/api/engagement', '/api/activation', '/api/retention',
    '/api/cohorts', '/api/revenue', '/api/feature-adoption', '/api/experiments', '/api/nps',
    '/api/support', '/api/customer-health', '/api/customer-health/workspaces'})
CACHEABLE_PREFIXES = ('/api/experiments/',)


def is_cacheable(path):
    return path in CACHEABLE_PATHS or path.startswith(CACHEABLE_PREFIXES)


def cache_key(path, query_string, data_version):
    """Every input that shapes a response: path, all query parameters (order-
    insensitive, repeats kept), and the data version. Request headers do not
    change response bodies (the API key only gates access)."""
    params = tuple(sorted(parse_qsl(query_string, keep_blank_values=True)))
    return (path, params, data_version)


def etag_for(body):
    """Weak ETag from the canonical JSON representation, ignoring meta.generated_at."""
    try:
        document = json.loads(body)
    except ValueError:
        canonical = body
    else:
        if isinstance(document, dict) and isinstance(document.get('meta'), dict):
            document['meta'].pop('generated_at', None)
        canonical = json.dumps(document, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False).encode()
    return 'W/"' + hashlib.sha256(canonical).hexdigest()[:32] + '"'


def etag_matches(if_none_match, etag):
    """RFC 9110 weak comparison against an If-None-Match header value."""
    if not if_none_match:
        return False
    if if_none_match.strip() == '*':
        return True

    def opaque(tag):
        tag = tag.strip()
        return tag[2:] if tag.startswith('W/') else tag
    return opaque(etag) in {opaque(t) for t in if_none_match.split(',') if t.strip()}


@dataclass
class Entry:
    body: bytes
    etag: str
    media_type: str
    expires: float


class ResponseCache:
    """Bounded in-process TTL + LRU cache. Used from the event loop only."""

    def __init__(self, ttl_s, max_entries, max_bytes, clock=time.monotonic):
        self.ttl_s, self.max_entries, self.max_bytes, self.clock = ttl_s, max_entries, max_bytes, clock
        self._entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = self.evictions = 0

    def __len__(self):
        return len(self._entries)

    def keys(self):
        return list(self._entries)

    def get(self, key):
        entry = self._entries.get(key)
        if entry is None or entry.expires <= self.clock():
            if entry is not None:
                self._drop(key)
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry

    def put(self, key, body, etag, media_type):
        if len(body) > self.max_bytes:
            return False
        if key in self._entries:
            self._drop(key)
        self._entries[key] = Entry(body, etag, media_type, self.clock() + self.ttl_s)
        self.bytes += len(body)
        while len(self._entries) > self.max_entries or self.bytes > self.max_bytes:
            self._drop(next(iter(self._entries)))
            self.evictions += 1
        return True

    def clear(self):
        self._entries.clear()
        self.bytes = 0

    def _drop(self, key):
        self.bytes -= len(self._entries.pop(key).body)


class DataVersion:
    """The current data_version, re-read from ops.pipeline_runs at most every ttl_s."""

    def __init__(self, ttl_s, clock=time.monotonic):
        self.ttl_s, self.clock = ttl_s, clock
        self.value, self.expires = None, float('-inf')
        self.lookups = 0

    async def get(self, engine):
        """The version, or None when there is no validated run or it cannot be read."""
        if self.clock() < self.expires:
            return self.value
        try:
            value = await anyio.to_thread.run_sync(self._read, engine)
        except Exception as exc:          # warehouse not built / unreachable: do not cache
            log.info('data_version_unavailable',
                     extra={'fields': {'error': redact(f'{type(exc).__name__}: {exc}')[:200]}})
            return None
        self.value, self.expires = value, self.clock() + self.ttl_s
        return value

    def _read(self, engine):
        self.lookups += 1
        with engine.connect() as conn:
            return system.data_version(conn)


def _set_state(status):
    state = request_state_var.get()
    if state is not None:
        state['cache'] = status


class CacheMiddleware:
    """Serves and stores cacheable responses; adds ETag / Cache-Control; answers 304."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'GET' or not is_cacheable(scope['path']):
            return await self.app(scope, receive, send)
        state = scope['app'].state
        headers = Headers(scope=scope)
        if_none_match = headers.get('if-none-match')
        query = scope.get('query_string', b'').decode('latin-1')
        cache = state.response_cache if self.settings.api_cache_enabled else None
        authorized = api_key_valid(self.settings, headers.get('x-api-key'))

        key = None
        if cache is not None and authorized:
            version = await state.data_version.get(getattr(state, 'engine', None))
            if version is not None:
                key = cache_key(scope['path'], query, version)
                entry = cache.get(key)
                if entry is not None:
                    _set_state('hit')
                    return await self._send(send, 200, entry.body, entry.etag, entry.media_type,
                                            if_none_match, 'HIT')
        _set_state('miss' if key is not None else 'bypass')

        started, chunks = {}, []

        async def capture(message):
            if message['type'] == 'http.response.start':
                started.update(message)
                media = Headers(raw=message.get('headers', [])).get('content-type', '')
                started['buffer'] = message['status'] == 200 and media.startswith(
                    'application/json')
                if not started['buffer']:          # errors etc.: pass through untouched
                    await send(message)
                return
            if not started.get('buffer'):
                await send(message)
                return
            chunks.append(message.get('body', b''))
            if message.get('more_body', False):
                return
            body = b''.join(chunks)
            etag = etag_for(body)
            media_type = Headers(raw=started.get('headers', [])).get('content-type')
            if cache is not None and authorized:
                stored_key = self._key_for_body(scope['path'], query, body)
                if stored_key is not None:
                    cache.put(stored_key, body, etag, media_type)
            await self._send(send, 200, body, etag, media_type, if_none_match,
                             'MISS' if key is not None else 'BYPASS',
                             base_headers=started.get('headers', []))

        await self.app(scope, receive, capture)

    @staticmethod
    def _key_for_body(path, query, body):
        """Store under the data_version the body itself reports, so key and content agree."""
        try:
            version = json.loads(body).get('meta', {}).get('data_version')
        except (ValueError, AttributeError):
            return None
        return None if version is None else cache_key(path, query, version)

    async def _send(self, send, status, body, etag, media_type, if_none_match, cache_status,
                    base_headers=()):
        validators = [(b'etag', etag.encode()),
                      (b'cache-control',
                       f'private, max-age={self.settings.api_cache_max_age_s}'.encode()),
                      (b'x-cache', cache_status.encode())]
        if etag_matches(if_none_match, etag):
            await send({'type': 'http.response.start', 'status': 304, 'headers': validators})
            await send({'type': 'http.response.body', 'body': b''})
            return
        kept = [(k, v) for k, v in base_headers
                if k.lower() not in (b'content-length', b'etag', b'cache-control', b'x-cache')]
        if not base_headers:
            kept = [(b'content-type', (media_type or 'application/json').encode())]
        await send({'type': 'http.response.start', 'status': status,
                    'headers': kept + validators + [(b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})
