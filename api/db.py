"""
Warehouse access for the API (PHASE_4_PLAN.md §6).

One pooled engine per process, created in the app lifespan. Every connection
logs in as the read-only API role and its session is read-only with statement,
lock and idle-in-transaction timeouts, so a request can neither write nor hang
behind a dbt table swap. Each request gets one read-only transaction
(get_conn), always rolled back.
"""
import time

from fastapi import Request
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import event

from api.context import db_stats_var
from pipeline.config import database_url

APPLICATION_NAME = 'connecthub-api'


def engine_url(settings):
    url = database_url(settings.postgres_db, user=settings.api_db_user,
                       password=settings.api_db_password.get_secret_value())
    return url.set(host=settings.postgres_host, port=settings.postgres_port)


def session_options(settings):
    return ' '.join([
        '-c default_transaction_read_only=on',
        f'-c statement_timeout={settings.api_statement_timeout_ms}',
        f'-c lock_timeout={settings.api_lock_timeout_ms}',
        '-c idle_in_transaction_session_timeout=10000',
    ])


def create_engine(settings):
    engine = sa_create_engine(
        engine_url(settings),
        pool_size=settings.api_db_pool_size,
        max_overflow=settings.api_db_max_overflow,
        pool_timeout=settings.api_db_pool_timeout_s,
        pool_recycle=settings.api_db_pool_recycle_s,
        pool_pre_ping=True,
        connect_args={'options': session_options(settings),
                      'application_name': APPLICATION_NAME,
                      'connect_timeout': settings.api_db_connect_timeout_s},
    )
    _instrument(engine)
    return engine


def _instrument(engine):
    """Count queries and database time per request (for the request log line)."""
    @event.listens_for(engine, 'before_cursor_execute')
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault('query_started', []).append(time.perf_counter())

    @event.listens_for(engine, 'after_cursor_execute')
    def _after(conn, cursor, statement, parameters, context, executemany):
        started = conn.info['query_started'].pop()
        stats = db_stats_var.get()
        if stats is not None:
            stats[0] += 1
            stats[1] += (time.perf_counter() - started) * 1000


def get_conn(request: Request):
    """FastAPI dependency: a connection inside a read-only transaction."""
    with request.app.state.engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()
