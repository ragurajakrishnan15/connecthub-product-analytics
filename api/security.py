"""
API-key authentication (API_AUTH_MODE=api_key; PHASE_4_PLAN.md §8.6).

Keys arrive in the X-API-Key header and are compared in constant time. With
API_AUTH_MODE=none (the local default, bound to 127.0.0.1) the check is a no-op.
Liveness and readiness stay open so container healthchecks work.
"""
import hmac

from fastapi import Request

from api.errors import APIError


def api_key_valid(settings, supplied):
    """True when authentication is off or `supplied` matches a configured key."""
    if settings.api_auth_mode == 'none':
        return True
    supplied = supplied or ''
    valid = False
    for key in settings.api_keys:   # check every key: no early exit on a match
        valid |= hmac.compare_digest(supplied.encode(), key.get_secret_value().encode())
    return bool(supplied) and valid


def require_api_key(request: Request):
    settings = request.app.state.settings
    if not api_key_valid(settings, request.headers.get('x-api-key')):
        raise APIError('unauthorized', 'a valid X-API-Key header is required',
                       headers={'WWW-Authenticate': 'ApiKey header="X-API-Key"'})
