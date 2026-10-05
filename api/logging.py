"""
JSON-lines logging for the API (one object per line on stdout).

Same conventions as pipeline/log.py: every line is passed through
pipeline.log.redact(), so secret environment values and passwords in
connection URLs are masked. Every record carries the request_id of the request
it belongs to (null outside a request).
"""
import json
import logging
import sys
import time

from api.context import request_id_var
from pipeline.log import redact

LOGGER = 'connecthub.api'


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created)) + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'event': record.getMessage(),
            'request_id': request_id_var.get(),
        }
        payload.update(getattr(record, 'fields', {}) or {})
        if record.exc_info:
            payload['error'] = self.formatException(record.exc_info).splitlines()[-1]
        return redact(json.dumps(payload, default=str))


def configure(level='INFO', stream=None):
    """Route the API's and uvicorn's loggers to one JSON handler (idempotent)."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    for name in (LOGGER, 'uvicorn', 'uvicorn.error'):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(level)
        logger.propagate = False
    # Requests are logged by api.middleware (with request IDs); uvicorn's own
    # access log would duplicate them without IDs.
    logging.getLogger('uvicorn.access').disabled = True
    return logging.getLogger(LOGGER)
