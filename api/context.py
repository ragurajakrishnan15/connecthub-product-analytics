"""Per-request context shared by middleware, logging, errors and the database layer."""
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar('request_id', default=None)
# [queries, milliseconds] spent in the database during the current request
db_stats_var: ContextVar[list | None] = ContextVar('db_stats', default=None)
