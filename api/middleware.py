"""
Request middleware (pure ASGI, outermost in the stack):

- request ID: accepts X-Request-ID if it looks like an ID, otherwise generates
  one; echoed in the response and in every log line and problem body
- one JSON log line per request: method, route template, status, duration,
  database queries and time
- security headers on every response (PHASE_4_PLAN.md §8.6)
- query strings longer than API_MAX_QUERY_STRING are refused (414)
- HEAD is served by the GET route without a body
- any unhandled exception becomes a 500 problem (logged, redacted)
"""
import logging
import re
import time
import uuid

from api.context import db_stats_var, request_id_var, request_state_var
from api.errors import problem_response
from pipeline.log import redact

log = logging.getLogger('connecthub.api')
_REQUEST_ID = re.compile(r'^[A-Za-z0-9-]{8,64}$')

SECURITY_HEADERS = {
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'no-referrer',
    'x-frame-options': 'DENY',
    'cross-origin-resource-policy': 'same-site',
}
API_CSP = "default-src 'none'; frame-ancestors 'none'"
# Swagger UI / ReDoc pages load their assets from jsdelivr (FastAPI's defaults)
DOCS_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data: https://fastapi.tiangolo.com "
            "https://cdn.redoc.ly; connect-src 'self'; worker-src blob:; frame-ancestors 'none'")


class RequestContextMiddleware:
    def __init__(self, app, max_query_string=2048, docs_paths=()):
        self.app = app
        self.max_query_string = max_query_string
        self.docs_paths = set(docs_paths)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        incoming = dict(scope['headers']).get(b'x-request-id', b'').decode('latin-1')
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        rid_token = request_id_var.set(request_id)
        stats = [0, 0.0]
        db_token = db_stats_var.set(stats)
        request_state = {}
        state_token = request_state_var.set(request_state)
        method = scope['method']
        if method == 'HEAD':
            scope = dict(scope, method='GET')
        csp = DOCS_CSP if scope['path'] in self.docs_paths else API_CSP
        started = time.perf_counter()
        state = {'status': 500, 'started': False}

        async def send_wrapper(message):
            if message['type'] == 'http.response.start':
                state['status'], state['started'] = message['status'], True
                headers = [(k, v) for k, v in message.get('headers', [])
                           if k.lower() not in (b'x-request-id',)]
                present = {k.lower() for k, _ in headers}
                headers.append((b'x-request-id', request_id.encode()))
                for name, value in {**SECURITY_HEADERS, 'content-security-policy': csp}.items():
                    if name.encode() not in present:
                        headers.append((name.encode(), value.encode()))
                message = {**message, 'headers': headers}
            elif message['type'] == 'http.response.body' and method == 'HEAD':
                message = {**message, 'body': b'', 'more_body': False}
            await send(message)

        try:
            if len(scope.get('query_string', b'')) > self.max_query_string:
                response = problem_response(
                    'uri-too-long', f'query string exceeds {self.max_query_string} bytes',
                    scope['path'])
                await response(scope, receive, send_wrapper)
            else:
                await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            log.error('unhandled_error', extra={'fields': {
                'path': scope['path'], 'error': redact(f'{type(exc).__name__}: {exc}')[:500]}})
            if state['started']:
                raise
            await problem_response('internal-error', 'an unexpected error occurred',
                                   scope['path'])(scope, receive, send_wrapper)
        finally:
            route = scope.get('route')
            log.info('request', extra={'fields': {
                'method': method,
                'route': getattr(route, 'path', None) or scope['path'],
                'status': state['status'],
                'duration_ms': round((time.perf_counter() - started) * 1000, 2),
                'db_queries': stats[0],
                'db_ms': round(stats[1], 2),
                'cache': request_state.get('cache'),
                'client': (scope.get('client') or (None,))[0],
            }})
            request_state_var.reset(state_token)
            db_stats_var.reset(db_token)
            request_id_var.reset(rid_token)
