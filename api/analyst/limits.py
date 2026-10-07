"""
Abuse and cost controls for the analyst route (PHASE_6_PLAN.md §4.4), all in process memory.

    RateLimiter  a sliding one-minute window per client, plus a cap on turns running at once
    DailyBudget  a model-token budget shared by every client that resets at 00:00 UTC; it is an
                 engine Budget, so a turn stops the moment the day's tokens are spent
    origin_allowed  the same-origin rule for a state-changing request

Nothing here reads a secret or the network. Counters are per worker process (API_WORKERS > 1
multiplies the limits, as with the response cache); they are not persisted.
"""
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from api.analyst.engine import Budget

MAX_CLIENTS = 10_000
MAX_CONCURRENT_TURNS = 4


class RateLimiter:
    """At most `limit` accepted requests per `window_s` seconds per client key, and at most
    MAX_CONCURRENT_TURNS turns in flight. acquire() returns (True, 0) or (False, retry_after_s)."""

    def __init__(self, limit, window_s=60.0, clock=time.monotonic, max_concurrent=MAX_CONCURRENT_TURNS):
        self.limit, self.window_s, self.max_concurrent = limit, window_s, max_concurrent
        self._clock, self._lock = clock, threading.Lock()
        self._hits, self._running = {}, 0

    def acquire(self, client):
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get(client, ()) if now - t < self.window_s]
            if len(hits) >= self.limit:
                self._hits[client] = hits
                return False, max(1, int(self.window_s - (now - hits[0])) + 1)
            if self._running >= self.max_concurrent:
                return False, 1
            hits.append(now)
            self._hits.pop(client, None)                  # re-insert last: dicts keep insertion order
            self._hits[client] = hits
            while len(self._hits) > MAX_CLIENTS:          # forget the least recently active client
                self._hits.pop(next(iter(self._hits)))
            self._running += 1
            return True, 0

    def release(self):
        with self._lock:
            self._running = max(0, self._running - 1)


def _utc_day(now=None):
    return (now or datetime.now(timezone.utc)).date()


class DailyBudget(Budget):
    """A Budget whose counters start again each UTC day."""

    def __init__(self, max_tokens, today=_utc_day):
        super().__init__(max_tokens=max_tokens)
        self._today, self._day = today, today()

    def _roll(self):                                      # call with the lock held
        day = self._today()
        if day != self._day:
            self._day, self.requests, self.tokens = day, 0, 0

    def charge(self, tokens):
        with self._lock:
            self._roll()
            self.requests += 1
            self.tokens += max(0, int(tokens))

    def exhausted(self):
        with self._lock:
            self._roll()
            return 'tokens' if self.tokens >= self.max_tokens else None

    def remaining_tokens(self):
        with self._lock:
            self._roll()
            return max(0, self.max_tokens - self.tokens)

    def seconds_until_reset(self, now=None):
        now = now or datetime.now(timezone.utc)
        midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
        return max(1, int(86400 - (now - midnight).total_seconds()))


def _netloc(origin):
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    return parts.netloc.lower() if parts.scheme in ('http', 'https') and parts.netloc else None


def origin_allowed(origin, host, allowed_origins):
    """The same-origin rule. A request with no Origin header (curl, scripts, server-side callers:
    nothing a browser sends for a cross-site POST) is allowed. A request with one needs both the
    Origin to be one of the configured origins (API_CORS_ORIGINS, which default to this server's
    own address) and the Host header to be the host of one of them, so a page on another site
    cannot get in with its own Origin, and a DNS-rebinding page cannot get in with a Host the
    server was never configured for. "null" and malformed origins are refused."""
    if origin is None:
        return True
    allowed = {o.lower().rstrip('/') for o in allowed_origins}
    if origin.lower().rstrip('/') not in allowed:
        return False
    hosts = {_netloc(o) for o in allowed}
    return bool(host) and host.lower() in hosts
