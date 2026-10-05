"""
Shared request-parameter rules.

Endpoints declare the query parameters they accept; anything else is rejected
with 400 invalid-parameter instead of being silently ignored
(PHASE_4_PLAN.md §8.3). Endpoint-specific parameter models (date ranges,
granularity, plan tier, pagination) are added with the data endpoints.
"""
from fastapi import Request

from api.errors import APIError


def allow_query_params(*allowed):
    """Dependency factory: 400 for any query parameter not in `allowed`."""
    allowed_set = set(allowed)

    def dependency(request: Request):
        unknown = sorted(set(request.query_params) - allowed_set)
        if unknown:
            expected = ', '.join(sorted(allowed_set)) or 'none'
            raise APIError('invalid-parameter',
                           f"unknown query parameter(s): {', '.join(unknown)}; accepted: {expected}",
                           errors=[{'loc': ['query', p], 'msg': 'unknown parameter',
                                    'type': 'unknown_parameter'} for p in unknown])
    return dependency
