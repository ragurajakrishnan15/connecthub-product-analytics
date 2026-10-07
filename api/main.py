"""
App factory: create_app(settings) builds the FastAPI application.

    python -m api                      run with uvicorn (see api/__main__.py)
    uvicorn api.main:create_app --factory

POST /api/analyst/chat is the only route that is not a GET (api/routers/analyst.py).

Middleware, outermost first: request context (IDs, logging, security headers,
500s) -> CORS -> GZip -> response cache / ETag (api/cache.py) -> exception
handlers -> routes.

With API_DASHBOARD_PATH set, GET / also serves the dashboard page with its own
Content-Security-Policy (api/dashboard.py); the page is outside the API contract.
"""
import time
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from api import __version__, errors
from api import logging as api_logging
from api.cache import CacheMiddleware, DataVersion, ResponseCache
from api.dashboard import dashboard_router, load_dashboard
from api.db import create_engine
from api.middleware import RequestContextMiddleware
from api.analyst.limits import DailyBudget, RateLimiter
from api.routers import analyst, analytics, health, meta
from api.settings import Settings

DOCS_URL, REDOC_URL, OPENAPI_URL = '/api/docs', '/api/redoc', '/api/openapi.json'
TAGS = [
    {'name': 'health', 'description': 'Liveness and readiness (no authentication).'},
    {'name': 'meta', 'description': 'Data window, freshness and dimension metadata.'},
    {'name': 'overview', 'description': 'Headline KPIs.'},
    {'name': 'engagement', 'description': 'Daily, weekly and monthly active users.'},
    {'name': 'activation', 'description': '14-day activation funnel by signup date.'},
    {'name': 'retention', 'description': 'Weekly retention cohorts.'},
    {'name': 'revenue', 'description': 'Monthly recurring revenue and movement.'},
    {'name': 'feature-adoption', 'description': 'Feature adoption curves and monthly usage.'},
    {'name': 'experiments', 'description': 'Persisted A/B experiment evaluations.'},
    {'name': 'nps', 'description': 'Net Promoter Score.'},
    {'name': 'support', 'description': 'Support tickets and AI voice-agent performance.'},
    {'name': 'customer-health', 'description': 'Workspace health scores and tiers.'},
    {'name': 'analyst', 'description': 'Grounded AI analyst (off unless configured).'},
]
DESCRIPTION = """Read-only business metrics from the ConnectHub analytics warehouse.

Every number comes from dbt models and Python analytics that the pipeline builds
and Great Expectations validates; definitions are in `docs/metric-definitions.md`.
Errors are RFC 9457 problem documents (`application/problem+json`).

Successful data responses carry a weak `ETag`; send it back in `If-None-Match` to get
`304 Not Modified` while the data is unchanged. Responses are cached per data version
(the last validated pipeline run, `meta.data_version`)."""


def create_app(settings: Settings | None = None, analyst_llm=None) -> FastAPI:
    """`analyst_llm` is the analyst's language-model client (api.analyst.llm.LlmClient). Tests pass a
    scripted fake; no real client exists yet, so the analyst route answers 503 without one."""
    settings = settings or Settings()
    log = api_logging.configure(settings.api_log_level)

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = create_engine(settings)
        # Requests wait in the event loop rather than in threads blocked on the pool.
        anyio.to_thread.current_default_thread_limiter().total_tokens = (
            settings.api_db_pool_size + settings.api_db_max_overflow + 2)
        log.info('startup', extra={'fields': {'version': __version__,
                                              'settings': settings.summary()}})
        try:
            yield
        finally:
            app.state.engine.dispose()
            log.info('shutdown')

    docs = settings.docs_enabled
    app = FastAPI(
        title='ConnectHub Analytics API', version=__version__, description=DESCRIPTION,
        openapi_tags=TAGS, lifespan=lifespan,
        docs_url=DOCS_URL if docs else None, redoc_url=REDOC_URL if docs else None,
        openapi_url=OPENAPI_URL if docs else None,
    )
    app.state.settings = settings
    app.state.started = time.monotonic()
    app.state.response_cache = ResponseCache(settings.api_cache_ttl_s,
                                             settings.api_cache_max_entries,
                                             settings.api_cache_max_bytes)
    app.state.data_version = DataVersion(settings.api_data_version_ttl_s)
    app.state.analyst_llm = analyst_llm
    app.state.analyst_limiter = RateLimiter(settings.analyst_rate_limit_per_min)
    app.state.analyst_budget = DailyBudget(settings.analyst_daily_token_budget)

    errors.install(app)
    app.include_router(health.router)
    app.include_router(meta.router)
    app.include_router(analytics.router)
    app.include_router(analyst.router)
    if settings.api_dashboard_path:
        app.state.dashboard = load_dashboard(settings.api_dashboard_path)
        app.include_router(dashboard_router(app.state.dashboard))

    # Innermost: caches the canonical (uncompressed) body; everything outside it
    # (GZip, CORS, request IDs, logging, security headers) applies to cache hits too.
    app.add_middleware(CacheMiddleware, settings=settings)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.api_cors_origins, allow_credentials=False,
        allow_methods=['GET', 'HEAD', 'OPTIONS'],
        allow_headers=['X-API-Key', 'X-Request-ID', 'If-None-Match'],
        expose_headers=['ETag', 'X-Request-ID'], max_age=600)
    app.add_middleware(RequestContextMiddleware,
                       max_query_string=settings.api_max_query_string,
                       docs_paths=(DOCS_URL, REDOC_URL) if docs else ())
    return app
