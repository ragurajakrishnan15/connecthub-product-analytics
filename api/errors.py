"""
Errors as RFC 9457 problem documents (application/problem+json).

Every error response has the same shape:
    {"type", "title", "status", "detail", "instance", "request_id", "errors"?}
`type` is a URN (urn:connecthub:problem:<slug>); the slugs are listed in
PROBLEMS. Bodies never contain SQL, stack traces, connection strings or
secrets: the full exception is logged (redacted) under the same request_id.
"""
import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.context import request_id_var
from pipeline.log import redact

log = logging.getLogger('connecthub.api')
MEDIA_TYPE = 'application/problem+json'

PROBLEMS = {  # slug -> (status, title)
    'validation-error': (422, 'Request validation failed'),
    'invalid-parameter': (400, 'Invalid parameter'),
    'invalid-range': (400, 'Invalid range'),
    'unauthorized': (401, 'Unauthorized'),
    'not-found': (404, 'Not found'),
    'experiment-not-found': (404, 'Experiment not found'),
    'method-not-allowed': (405, 'Method not allowed'),
    'uri-too-long': (414, 'Query string too long'),
    'data-not-ready': (503, 'Warehouse data not ready'),
    'database-unavailable': (503, 'Database unavailable'),
    'pool-exhausted': (503, 'Server busy'),
    'warehouse-busy': (503, 'Warehouse busy'),
    'query-timeout': (504, 'Query timed out'),
    'internal-error': (500, 'Internal server error'),
}
RETRYABLE = {'database-unavailable', 'pool-exhausted', 'warehouse-busy', 'data-not-ready'}

# PostgreSQL SQLSTATE codes we translate (psycopg2 exposes them as pgcode)
UNDEFINED_TABLE, INSUFFICIENT_PRIVILEGE = '42P01', '42501'
QUERY_CANCELED, LOCK_NOT_AVAILABLE = '57014', '55P03'


class APIError(Exception):
    """An expected failure with a problem slug, e.g. APIError('invalid-range', '...')."""

    def __init__(self, slug, detail, errors=None, headers=None):
        super().__init__(detail)
        if slug not in PROBLEMS:
            raise ValueError(f'unknown problem slug {slug!r}')
        self.slug, self.detail, self.errors, self.headers = slug, detail, errors, headers or {}


def problem_response(slug, detail, instance, errors=None, headers=None):
    status, title = PROBLEMS[slug]
    body = {'type': f'urn:connecthub:problem:{slug}', 'title': title, 'status': status,
            'detail': detail, 'instance': instance, 'request_id': request_id_var.get()}
    if errors:
        body['errors'] = errors
    headers = dict(headers or {})
    if slug in RETRYABLE:
        headers.setdefault('Retry-After', '5')
    return JSONResponse(body, status_code=status, headers=headers, media_type=MEDIA_TYPE)


def classify_db_error(exc):
    """(slug, detail) for a SQLAlchemy/DBAPI error."""
    if isinstance(exc, sa_exc.TimeoutError):          # connection pool exhausted
        return 'pool-exhausted', 'all database connections are busy; retry shortly'
    code = getattr(getattr(exc, 'orig', None), 'pgcode', None)
    if code == UNDEFINED_TABLE:
        return 'data-not-ready', ('a required warehouse table does not exist; '
                                  'run the pipeline (python -m pipeline run)')
    if code == INSUFFICIENT_PRIVILEGE:
        return 'data-not-ready', ('the API database role cannot read a required table; '
                                  'run python -m api.provision')
    if code == QUERY_CANCELED:
        return 'query-timeout', 'the query exceeded the statement timeout'
    if code == LOCK_NOT_AVAILABLE:
        return 'warehouse-busy', 'the warehouse is being rebuilt; retry shortly'
    if isinstance(exc, sa_exc.OperationalError) or isinstance(exc, sa_exc.InterfaceError):
        return 'database-unavailable', 'the warehouse database is not reachable'
    return 'internal-error', 'an unexpected database error occurred'


def _validation_errors(exc):
    return [{'loc': [str(p) for p in e.get('loc', ())], 'msg': e.get('msg', ''),
             'type': e.get('type', '')} for e in exc.errors()]


def install(app):
    """Register the problem+json handlers on a FastAPI app."""

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return problem_response(exc.slug, exc.detail, request.url.path, exc.errors, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return problem_response('validation-error', 'one or more parameters are invalid',
                                request.url.path, _validation_errors(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        slug = {404: 'not-found', 405: 'method-not-allowed', 401: 'unauthorized'}.get(
            exc.status_code)
        if slug is None:
            slug = 'internal-error' if exc.status_code >= 500 else 'invalid-parameter'
        detail = exc.detail if isinstance(exc.detail, str) and slug != 'internal-error' \
            else PROBLEMS[slug][1]
        return problem_response(slug, detail, request.url.path, headers=exc.headers)

    @app.exception_handler(sa_exc.SQLAlchemyError)
    async def _database(request: Request, exc: sa_exc.SQLAlchemyError):
        slug, detail = classify_db_error(exc)
        level = logging.ERROR if slug == 'internal-error' else logging.WARNING
        log.log(level, 'database_error', extra={'fields': {
            'problem': slug, 'error': redact(f'{type(exc).__name__}: {exc}')[:500]}})
        return problem_response(slug, detail, request.url.path)
