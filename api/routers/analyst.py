"""POST /api/analyst/chat (PHASE_6_PLAN.md §4).

    client request -> checks -> chat engine -> approved tools (read-only pool) -> grounding -> response

Checks, in order: API key (the existing X-API-Key rule), same origin, no query string, analyst
configured (503), JSON content type, rate limit, daily token budget, request size, then the body is
parsed and the history validated. Nothing is stored: the browser sends the conversation each time.

The response carries plain text only, the sources the answer was checked against, and the
grounding status. Errors are problem documents with the request id; none contains an exception
message, SQL, a credential or a stack trace (the cause is logged, redacted).
"""
import json
import logging
import re
from functools import partial

import anyio.to_thread
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from api.analyst import grounding
from api.analyst.engine import HistoryError, validate_history
from api.analyst.limits import origin_allowed
from api.context import request_id_var
from api.errors import APIError
from api.params import allow_query_params
from api.schemas.analyst import CHAT_REQUEST_SCHEMA, ChatResponse
from api.schemas.common import problem_responses
from api.security import require_api_key
from pipeline.log import redact

log = logging.getLogger('connecthub.api.analyst')

NO_STORE = {'Cache-Control': 'no-store'}
MAX_SOURCE_LIST = 10
MAX_SOURCE_TEXT = 300
_TAG_SHAPED = re.compile(r'<(?=[A-Za-z/!?])')
WITHHELD = {'ungrounded': 'Some numbers in the answer could not be matched to the data, so the '
                          'answer was withheld.',
            'tool-limit': 'The analyst reached its limit of data lookups for one question and '
                          'could not finish. Ask a narrower question.'}
_GENERIC = 'an unexpected error occurred'


def require_same_origin(request: Request):
    """A browser sends Origin on every cross-site POST; accept only the configured origins."""
    settings = request.app.state.settings
    if not origin_allowed(request.headers.get('origin'), request.headers.get('host'),
                          settings.api_cors_origins):
        raise APIError('forbidden-origin', 'this request came from an origin that is not allowed')


router = APIRouter(prefix='/api/analyst', tags=['analyst'],
                   dependencies=[Depends(require_api_key), Depends(require_same_origin),
                                 Depends(allow_query_params())])


def _is_json(request):
    return request.headers.get('content-type', '').split(';')[0].strip().lower() == 'application/json'


def _plain_text(answer):
    """The answer with every tag-shaped '<' replaced by a full-width one: it cannot open an HTML
    tag or comment even if a client inserts it as markup."""
    return _TAG_SHAPED.sub('＜', answer)


def _clip(value):
    return value if len(value) <= MAX_SOURCE_TEXT else value[:MAX_SOURCE_TEXT - 1] + '…'


def _sources(report):
    """The consulted tool results, for the page. The Python service path stays server-side."""
    supported = {}
    for claim in report.claims:
        for call_id in claim.get('source_ids', ()):
            supported[call_id] = supported.get(call_id, 0) + 1
    return [{'id': s['call_id'], 'tool': s['tool'], 'endpoint': s['endpoint'],
             'arguments': s['arguments'], 'data_version': s['data_version'], 'as_of': s['as_of'],
             'relations': [_clip(str(r)) for r in s['relations'][:MAX_SOURCE_LIST]],
             'caveats': [_clip(str(c)) for c in s['caveats'][:MAX_SOURCE_LIST]],
             'truncated': s['truncated'], 'supported_claims': supported.get(s['call_id'], 0)}
            for s in report.sources if not s['conflicted']]


def _grounding(report, status):
    if report is None:
        return {'status': 'not_checked', 'claims_checked': 0, 'claims_derived': 0,
                'unverified_count': 0, 'caveats': [], 'dates_not_in_evidence': []}
    return {'status': status, 'claims_checked': len(report.claims),
            'claims_derived': sum(c['status'] == 'derived' for c in report.claims),
            'unverified_count': len(report.unverified_numbers),
            'caveats': report.caveats[:MAX_SOURCE_LIST],
            'dates_not_in_evidence': report.dates_not_in_evidence[:MAX_SOURCE_LIST]}


def _usage(result):
    return {'requests': result.usage.get('requests', 0),
            'tool_calls': result.limits.get('tool_calls_used', 0),
            'total_tokens': result.usage.get('total_tokens', 0)}


def _dump(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _fit(payload, cap):
    """Serialise payload; if it exceeds `cap` bytes drop source detail, then shorten the answer."""
    body = _dump(payload)
    if len(body.encode()) <= cap:
        return body
    payload = {**payload, 'sources': [{**s, 'arguments': {}, 'caveats': [], 'relations': []}
                                      for s in payload['sources']]}
    body = _dump(payload)
    while len(body.encode()) > cap and payload['answer']:
        payload['answer'] = payload['answer'][:len(payload['answer']) // 2]
        body = _dump(payload)
    return body


async def _read_body(request, cap):
    declared = request.headers.get('content-length')
    if declared is not None:
        if not declared.isdigit():
            raise APIError('invalid-parameter', 'Content-Length is not a number')
        if int(declared) > cap:
            raise APIError('payload-too-large', f'the request body is larger than {cap} bytes')
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise APIError('payload-too-large', f'the request body is larger than {cap} bytes')
        chunks.append(chunk)
    return b''.join(chunks)


def _parse(body, settings):
    """The validated message list, or an APIError. Anything but {"messages": [...]} is refused."""
    if not body.strip():
        raise APIError('validation-error', 'the request body is empty',
                       [{'loc': ['body'], 'msg': 'a JSON body is required', 'type': 'missing'}])
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        raise APIError('invalid-parameter', 'the request body is not valid JSON') from None
    if not isinstance(parsed, dict):
        raise APIError('validation-error', 'the request body must be a JSON object',
                       [{'loc': ['body'], 'msg': 'expected an object', 'type': 'invalid_body'}])
    extra = sorted(str(k)[:24] for k in parsed if k != 'messages')
    if extra or 'messages' not in parsed:
        problems = [{'loc': ['body', k], 'msg': 'unknown field; only "messages" is accepted',
                     'type': 'extra_forbidden'} for k in extra] or \
                   [{'loc': ['body', 'messages'], 'msg': 'field required', 'type': 'missing'}]
        raise APIError('validation-error', 'the request body is invalid', problems)
    try:
        validate_history(parsed['messages'], settings)
    except HistoryError as exc:
        slug = 'payload-too-large' if exc.code in ('message-too-long', 'conversation-too-long') \
            else 'validation-error'
        raise APIError(slug, exc.message,
                       [{'loc': ['body', 'messages'], 'msg': exc.message, 'type': exc.code}]) from None
    return parsed['messages']


def _respond(grounded, settings, state):
    result, report = grounded.result, grounded.report
    message = (result.error or {}).get('message') or 'the analyst could not answer'
    if result.status == 'budget_exhausted':
        raise APIError('budget-exhausted', message,
                       headers={'Retry-After': str(state.analyst_budget.seconds_until_reset())})
    if result.status == 'timeout':
        raise APIError('analyst-timeout', message)
    if result.status in ('upstream_error', 'empty_response'):
        raise APIError('analyst-upstream-error', message)
    if result.status == 'rejected':
        raise APIError('validation-error', message)
    if report is not None and 'grounding-error' in report.caveats:
        log.error('analyst_grounding_error')
        raise APIError('internal-error', _GENERIC)
    base = {'dataset': settings.api_dataset_label, 'request_id': request_id_var.get(),
            'usage': _usage(result), 'format': 'text/plain',
            'sources': _sources(report) if report is not None else []}
    if result.status == 'answered':
        payload = {**base, 'status': 'answered', 'answer': _plain_text(result.answer),
                   'reason': None, 'notice': None, 'grounding': _grounding(report, 'verified')}
    elif result.status in ('ungrounded', 'tool_limit'):
        reason = 'ungrounded' if result.status == 'ungrounded' else 'tool-limit'
        payload = {**base, 'status': 'withheld', 'answer': None, 'reason': reason,
                   'notice': WITHHELD[reason], 'grounding': _grounding(report, 'rejected')}
    else:                                                          # cancelled: nothing cancels here
        raise APIError('internal-error', _GENERIC)
    return Response(_fit(payload, settings.analyst_max_response_bytes),
                    media_type='application/json', headers=NO_STORE)


@router.post('/chat', response_model=ChatResponse,
             responses=problem_responses(400, 401, 403, 413, 415, 422, 429, 500, 502, 503, 504),
             openapi_extra={'requestBody': {'required': True, 'content': {'application/json': {
                 'schema': CHAT_REQUEST_SCHEMA}}}},
             summary='Ask the analyst a question about the dashboard data')
async def chat(request: Request):
    """Answers from the dashboard's own data. The model may only call the approved read-only tools;
    every number in the answer is then checked against what those tools returned, and an answer
    with a number that no tool result supports is withheld (`status: withheld`).

    The server is stateless: send the conversation so far (user and assistant text only) each
    time. 503 `analyst-not-configured` until the analyst is enabled and configured; 429 when the
    per-client rate limit or the daily token budget is reached; 413 or 422 for a request that is
    too large or malformed. The answer is plain text."""
    state, settings = request.app.state, request.app.state.settings
    llm = getattr(state, 'analyst_llm', None)
    if not settings.analyst_ready or llm is None:
        raise APIError('analyst-not-configured', 'the analyst is not enabled or not configured')
    if not _is_json(request):
        raise APIError('unsupported-media-type', 'send the request as application/json')
    allowed, retry = state.analyst_limiter.acquire(request.client.host if request.client else '-')
    if not allowed:
        raise APIError('rate-limited', 'too many analyst requests; retry shortly',
                       headers={'Retry-After': str(retry)})
    try:
        if state.analyst_budget.exhausted():
            raise APIError('budget-exhausted', 'the analyst has used its daily allowance',
                           headers={'Retry-After': str(state.analyst_budget.seconds_until_reset())})
        messages = _parse(await _read_body(request, settings.analyst_max_request_bytes), settings)
        try:
            grounded = await anyio.to_thread.run_sync(partial(
                grounding.run_grounded_chat, messages, policy='reject', llm=llm,
                engine=state.engine, settings=settings, budget=state.analyst_budget,
                turn_timeout_s=settings.analyst_turn_timeout_s))
        except Exception as exc:                                   # the cause is logged, never returned
            log.error('analyst_route_error', extra={'fields': {
                'error': redact(f'{type(exc).__name__}: {exc}')[:300]}})
            raise APIError('internal-error', _GENERIC) from None
        return _respond(grounded, settings, state)
    finally:
        state.analyst_limiter.release()
