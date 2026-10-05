"""
Structured (JSON lines) logging for pipeline runs.

Every record carries run_id and step. Secret values from the environment
(passwords, keys, tokens) and password fields in connection URLs are masked
before anything is written.
"""
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager

from pipeline.config import secret_values

_URL_PASSWORD = re.compile(r'(://[^:/@\s]+:)[^@\s]+(@)')


def redact(text):
    text = _URL_PASSWORD.sub(r'\1***\2', str(text))
    for secret in secret_values():
        text = text.replace(secret, '***')
    return text


def new_run_id():
    return os.environ.get('PIPELINE_RUN_ID') or \
        time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created)) + 'Z',
            'level': record.levelname,
            'run_id': getattr(record, 'run_id', None),
            'step': getattr(record, 'step', None),
            'event': record.getMessage(),
        }
        payload.update(getattr(record, 'fields', {}) or {})
        if record.exc_info:
            payload['error'] = self.formatException(record.exc_info).splitlines()[-1]
        return redact(json.dumps(payload, default=str))


def get_logger(run_id, step=None, stream=None):
    """A logger adapter that stamps run_id/step on every record."""
    logger = logging.getLogger('connecthub.pipeline')
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return StepLogger(logger, run_id, step)


class StepLogger:
    def __init__(self, logger, run_id, step):
        self.logger, self.run_id, self.step = logger, run_id, step

    def _log(self, level, event, exc_info=None, **fields):
        self.logger.log(level, event, exc_info=exc_info,
                        extra={'run_id': self.run_id, 'step': self.step, 'fields': fields})

    def info(self, event, **fields):
        self._log(logging.INFO, event, **fields)

    def error(self, event, exc_info=None, **fields):
        self._log(logging.ERROR, event, exc_info=exc_info, **fields)

    @contextmanager
    def timed(self, event, **fields):
        """Log start/end of a block with duration; logs and re-raises on failure."""
        start = time.perf_counter()
        self.info(f'{event}.start', **fields)
        try:
            yield
        except Exception as exc:
            self.error(f'{event}.failed', duration_s=round(time.perf_counter() - start, 2),
                       error=f'{type(exc).__name__}: {exc}')
            raise
        self.info(f'{event}.done', duration_s=round(time.perf_counter() - start, 2), **fields)
