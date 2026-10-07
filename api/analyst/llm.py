"""
The provider-neutral boundary between the chat engine and a language model (PHASE_6_PLAN.md step 3).

The engine speaks only these types. A concrete client (a provider adapter, added later; a scripted
fake in the tests) turns an LlmRequest into a provider call and the answer back into an
LlmResponse. The engine never sees a key, a URL or an SDK object, and a client never sees the
database: it can only ask for the tools named in request.tools.

Messages handed to a client are plain dicts with one of three shapes:
    {"role": "user" | "assistant", "text": str}
    {"role": "assistant", "text": None, "tool_calls": [{"id", "name", "arguments"}]}
    {"role": "tool", "results": [{"id", "name", "content": <JSON text of a tool result>}]}
There is no "system" message in the list: the server's instructions travel only in request.system.
"""
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    """A tool the model asked for. Both fields are untrusted model output."""
    name: Any
    arguments: Any


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self):
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class LlmResponse:
    """What the model said: an answer (text), tool calls, or neither (an empty response)."""
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None          # None: the engine estimates it so budgets still apply


@dataclass(frozen=True)
class LlmRequest:
    system: str
    messages: tuple[dict, ...]
    tools: tuple[dict, ...]             # name, description, JSON Schema: the approved tools only
    max_output_tokens: int
    timeout_s: float


class LlmError(Exception):
    """A failure the client already understands. `kind` is one of the KINDS below; the message is
    for logs only and is never shown to the user or the model."""
    KINDS = ('timeout', 'rate_limited', 'unavailable', 'bad_response', 'refused')

    def __init__(self, kind, message=''):
        super().__init__(message or kind)
        if kind not in self.KINDS:
            raise ValueError(f'unknown LlmError kind {kind!r}')
        self.kind = kind


class LlmClient(Protocol):
    def generate(self, request: LlmRequest) -> LlmResponse:
        """One model call. May raise LlmError (or TimeoutError for a timeout)."""
