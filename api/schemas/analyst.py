"""Request and response shapes of POST /api/analyst/chat (PHASE_6_PLAN.md §4.1).

The route parses the request body itself (to cap its size and answer with problem documents), so
ChatRequest documents the contract in OpenAPI and is not used to validate it."""
from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal['user', 'assistant'] = Field(
        description='Only user and assistant are accepted. System, developer, tool and function '
                    'messages are refused: the server writes its own instructions and tool results.')
    content: str = Field(description='Plain text. Limits: ANALYST_MAX_MESSAGE_CHARS per user message.')


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        min_length=1, description='The conversation so far, oldest first, alternating user and '
                                  'assistant, starting and ending with a user message. At most '
                                  'ANALYST_MAX_HISTORY_TURNS messages. The server stores nothing.')


class ChatSource(BaseModel):
    """One tool result the answer was checked against."""
    id: str = Field(description='Identifies the tool call within this response')
    tool: str
    endpoint: str = Field(description='The dashboard endpoint the same data comes from')
    arguments: dict = Field(description='The arguments the tool ran with, after defaults')
    data_version: str | None
    as_of: str | None
    relations: list[str] = Field(description='Warehouse relations the result was built from')
    caveats: list[str]
    truncated: bool = Field(description='A list in the result was cut to fit the size limit')
    supported_claims: int = Field(description='Numbers in the answer that this result supports')


class ChatGrounding(BaseModel):
    status: Literal['verified', 'rejected', 'not_checked'] = Field(
        description='verified: every number in the answer matched a tool result. rejected: some '
                    'number did not, and the answer was withheld. not_checked: no answer to check.')
    claims_checked: int
    claims_derived: int = Field(description='Numbers computed from tool results (a change, a total)')
    unverified_count: int
    caveats: list[str]
    dates_not_in_evidence: list[str]


class ChatUsage(BaseModel):
    requests: int
    tool_calls: int
    total_tokens: int


class ChatResponse(BaseModel):
    status: Literal['answered', 'withheld']
    answer: str | None = Field(description='Plain text, never HTML. Null when withheld.')
    format: Literal['text/plain'] = 'text/plain'
    reason: Literal['ungrounded', 'tool-limit'] | None = Field(
        None, description='Why the answer was withheld')
    notice: str | None = None
    grounding: ChatGrounding
    sources: list[ChatSource]
    usage: ChatUsage
    dataset: str
    request_id: str | None


def _inline(node, defs):
    if isinstance(node, dict):
        if '$ref' in node:
            return _inline(defs[node['$ref'].rsplit('/', 1)[1]], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k != '$defs'}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


_raw = ChatRequest.model_json_schema()
# The request body is parsed by the route, so its schema is written into the operation (inlined,
# no $defs) instead of being picked up from a parameter.
CHAT_REQUEST_SCHEMA = _inline(_raw, _raw.get('$defs', {}))
