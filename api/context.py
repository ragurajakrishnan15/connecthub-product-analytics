"""Per-request context shared by middleware, logging, errors and the database layer."""
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar('request_id', default=None)
# [queries, milliseconds] spent in the database during the current request
db_stats_var: ContextVar[list | None] = ContextVar('db_stats', default=None)
# Mutable per-request facts for the request log line (e.g. {'cache': 'hit'})
request_state_var: ContextVar[dict | None] = ContextVar('request_state', default=None)
