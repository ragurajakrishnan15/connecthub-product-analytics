"""Liveness (no I/O) and readiness (database, required relations, a validated run)."""
import logging
import time

from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from api import __version__
from api.errors import classify_db_error
from api.repositories import system
from api.schemas.health import (DatabaseCheck, Liveness, PipelineCheck, Readiness,
                                ReadinessChecks, RelationsCheck)
from pipeline.log import redact

log = logging.getLogger('connecthub.api')


def liveness(started_monotonic):
    return Liveness(status='ok', version=__version__,
                    uptime_s=round(time.monotonic() - started_monotonic, 1))


def readiness(engine, settings):
    """Readiness report; never raises. Ready only if every check passes."""
    relations = pipeline = data_end = None
    try:
        with engine.connect() as conn, conn.begin():
            conn.execute(text(f'SET LOCAL statement_timeout = {int(settings.api_ready_timeout_ms)}'))
            database = DatabaseCheck(ok=True, latency_ms=system.ping(conn))
            missing, not_readable = system.relation_status(conn)
            relations = RelationsCheck(ok=not missing and not not_readable,
                                       required=len(system.REQUIRED_RELATIONS),
                                       missing=missing, not_readable=not_readable)
            unusable = set(missing) | set(not_readable)
            if 'ops.pipeline_runs' not in unusable:
                run = system.last_successful_run(conn)
                pipeline = PipelineCheck(ok=run is not None,
                                         last_success_run_id=run and run['run_id'],
                                         last_success_at=run and run['finished_at'])
            else:
                pipeline = PipelineCheck(ok=False, last_success_run_id=None, last_success_at=None)
            if 'gold.fct_daily_active_users' not in unusable:
                data_end = conn.execute(text(
                    'SELECT MAX(event_date) FROM gold.fct_daily_active_users')).scalar()
    except sa_exc.SQLAlchemyError as exc:
        slug, _ = classify_db_error(exc)
        log.warning('readiness_check_failed', extra={'fields': {
            'problem': slug, 'error': redact(f'{type(exc).__name__}: {exc}')[:300]}})
        database = DatabaseCheck(ok=False, error=slug)
        relations = pipeline = None
    ok = database.ok and relations is not None and relations.ok and pipeline is not None \
        and pipeline.ok
    return Readiness(status='ready' if ok else 'not_ready',
                     checks=ReadinessChecks(database=database, relations=relations,
                                            pipeline=pipeline),
                     data_end=data_end)
