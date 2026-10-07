"""
The Gemini provider (PHASE_6_PLAN.md §5, step 7): an LlmClient that turns the chat engine's
provider-neutral request into one Google Gen AI call and the reply back into an LlmResponse.

    chat engine -> LlmClient.generate(LlmRequest) -> GeminiClient -> transport -> google-genai

The engine knows nothing of Gemini. This module knows nothing of the database, the tools' code or
the route: it is handed the fixed system text, the transcript the engine built, and the
declarations of the approved tools (api.analyst.tools.declarations()), and it can ask the model
for those tools and no others. Every tool call comes back to the engine, which validates it and
runs it through tools.run_tool; the model never receives a callable (automatic function calling
is off) and there is no second query path.

Boundaries
  * The key is passed in by the factory (settings.analyst_credential(), which reads the process
    environment through the Settings class) and lives only inside the SDK client. This module
    never logs it, formats it or puts it in a message; an exception from the SDK is mapped to an
    LlmError whose text is fixed (kind and HTTP status only), so no SDK message, header, URL or
    prompt can reach a log line or a response.
  * One generate() is exactly one HTTP request: SDK retries are switched off, so the engine's
    request budget and the Budget's counts are the real number of upstream requests.
  * Per request the engine supplies the output-token cap and the timeout; temperature is 0.
  * `transport` is a one-method seam (generate(model, contents, config)); tests give it a fake that
    returns real SDK response objects, so nothing in the tests uses the network.
"""
import base64
import binascii
import json
import logging

from api.analyst.llm import LlmError, LlmRequest, LlmResponse, ToolCall, Usage

log = logging.getLogger('connecthub.api.analyst')

TEMPERATURE = 0.0
MAX_TOOL_CALLS_IN_REPLY = 16          # a reply asking for more than this is treated as malformed
_SCHEMA_KEYS = {'description': 'description', 'enum': 'enum', 'minimum': 'minimum', 'maximum': 'maximum',
                'maxLength': 'max_length', 'maxItems': 'max_items', 'pattern': 'pattern'}
# A reply that ended for one of these reasons is never an answer, whatever text came with it: a
# filtered reply may be partial or altered, so it is refused (the engine shows fixed text).
_REFUSED = {'SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'LANGUAGE', 'OTHER',
            'IMAGE_SAFETY', 'IMAGE_PROHIBITED_CONTENT', 'IMAGE_RECITATION', 'IMAGE_OTHER', 'NO_IMAGE'}
_MALFORMED = {'MALFORMED_FUNCTION_CALL', 'UNEXPECTED_TOOL_CALL', 'TOO_MANY_TOOL_CALLS'}


def sdk_available():
    try:
        import google.genai  # noqa: F401
        return True
    except Exception:
        return False


class SdkTransport:
    """The real transport: one google-genai client. Constructing it makes no request."""

    def __init__(self, api_key):
        from google import genai
        self._client = genai.Client(api_key=api_key)

    def generate(self, model, contents, config):
        return self._client.models.generate_content(model=model, contents=contents, config=config)

    def __repr__(self):
        return 'SdkTransport(api_key=***)'


# --- request -------------------------------------------------------------------------------------------------------

def _schema(node):
    """A tool's JSON Schema as the subset Gemini's Schema accepts. Unsupported keywords (format,
    default, additionalProperties) are dropped: the tool layer validates every argument strictly
    anyway, and the description already states the format."""
    out = {'type': str(node.get('type', 'string')).upper()}
    for source, target in _SCHEMA_KEYS.items():
        if source in node:
            out[target] = node[source]
    if isinstance(node.get('items'), dict):
        out['items'] = _schema(node['items'])
    if isinstance(node.get('properties'), dict) and node['properties']:
        out['properties'] = {name: _schema(prop) for name, prop in node['properties'].items()}
    if node.get('required'):
        out['required'] = list(node['required'])
    return out


def _declarations(types, tools):
    declarations = []
    for tool in tools:
        parameters = tool.get('parameters') or {}
        declaration = {'name': tool['name'], 'description': tool.get('description', '')}
        if parameters.get('properties'):                  # Gemini rejects an empty OBJECT schema
            declaration['parameters'] = _schema(parameters)
        declarations.append(types.FunctionDeclaration(**declaration))
    return declarations


def _function_response(content):
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        parsed = {'output': str(content)}
    return parsed if isinstance(parsed, dict) else {'output': parsed}


def _contents(types, messages):
    """The engine's transcript as Gemini Content objects. Roles: user -> "user", assistant ->
    "model", tool results -> a "user" content of function responses."""
    contents = []
    for message in messages:
        role = message.get('role')
        if role in ('user', 'assistant') and isinstance(message.get('text'), str):
            contents.append(types.Content(role='user' if role == 'user' else 'model',
                                          parts=[types.Part(text=message['text'])]))
        elif role == 'assistant' and message.get('text') is None and isinstance(message.get('tool_calls'), list):
            parts = []
            for tool_call in message['tool_calls']:
                part = {'function_call': types.FunctionCall(name=tool_call['name'],
                                                            args=tool_call.get('arguments') or {})}
                state = tool_call.get('provider_state')
                if isinstance(state, str):                # the thought signature this model issued
                    try:
                        part['thought_signature'] = base64.b64decode(state, validate=True)
                    except (binascii.Error, ValueError):
                        pass
                parts.append(types.Part(**part))
            contents.append(types.Content(role='model', parts=parts))
        elif role == 'tool' and isinstance(message.get('results'), list):
            contents.append(types.Content(role='user', parts=[
                types.Part(function_response=types.FunctionResponse(
                    name=result['name'], response=_function_response(result['content'])))
                for result in message['results']]))
        else:
            raise LlmError('bad_response', 'the transcript has a message the provider cannot send')
    return contents


# --- response ------------------------------------------------------------------------------------------------------------

def _is_thought(part):
    return bool(getattr(part, 'thought', False))


def _name(value):
    return getattr(value, 'name', None) or str(value or '')


def _usage(metadata):
    if metadata is None:
        return None
    prompt = (getattr(metadata, 'prompt_token_count', None) or 0) + (getattr(metadata, 'tool_use_prompt_token_count', None) or 0)
    produced = (getattr(metadata, 'response_token_count', None) or getattr(metadata, 'candidates_token_count', None) or 0) \
        + (getattr(metadata, 'thoughts_token_count', None) or 0)
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (prompt, produced)):
        return None
    return Usage(prompt, produced) if prompt or produced else None


def _parse(response):
    feedback = getattr(response, 'prompt_feedback', None)
    if feedback is not None and getattr(feedback, 'block_reason', None):
        raise LlmError('refused', 'the prompt was blocked')
    candidates = getattr(response, 'candidates', None) or []
    usage = _usage(getattr(response, 'usage_metadata', None))
    if not candidates:
        raise LlmError('bad_response', 'the reply has no candidate')
    candidate = candidates[0]
    reason = _name(getattr(candidate, 'finish_reason', None)).rsplit('.', 1)[-1]
    if reason in _REFUSED:
        raise LlmError('refused', 'the model declined to answer')
    if reason in _MALFORMED:
        raise LlmError('bad_response', 'the reply is a malformed tool call')
    parts = list(getattr(getattr(candidate, 'content', None), 'parts', None) or [])
    calls, texts = [], []
    for part in parts:
        function_call = getattr(part, 'function_call', None)
        if function_call is not None and getattr(function_call, 'name', None):
            args = getattr(function_call, 'args', None)
            signature = getattr(part, 'thought_signature', None)
            calls.append(ToolCall(function_call.name, dict(args) if isinstance(args, dict) else {},
                                  base64.b64encode(signature).decode('ascii')
                                  if isinstance(signature, (bytes, bytearray)) and signature else None))
        elif isinstance(getattr(part, 'text', None), str) and not _is_thought(part):
            texts.append(part.text)
    if len(calls) > MAX_TOOL_CALLS_IN_REPLY:
        raise LlmError('bad_response', 'the reply asks for too many tools')
    if calls:
        return LlmResponse(tool_calls=tuple(calls), usage=usage)
    text = ''.join(texts).strip()
    return LlmResponse(text=text or None, usage=usage)


# --- failures --------------------------------------------------------------------------------------------------------------------

def _status(exc):
    code = getattr(exc, 'code', None) or getattr(exc, 'status_code', None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def map_exception(exc):
    """An LlmError with fixed text for any exception from the SDK or the HTTP layer. Nothing from
    the exception (message, URL, headers, body) is kept except the class's role and an HTTP status."""
    if isinstance(exc, LlmError):
        return exc
    status = _status(exc)
    try:
        import httpx
        timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException))
    except Exception:
        timed_out = isinstance(exc, TimeoutError)
    if timed_out or status in (408, 504):
        return LlmError('timeout', 'the model request timed out')
    if status in (401, 403):
        return LlmError('auth', f'the model service refused the credentials (status {status})')
    if status == 429:
        return LlmError('rate_limited', 'the model service rate-limited the request (status 429)')
    if status is not None and 400 <= status < 500:
        return LlmError('bad_response', f'the model service rejected the request (status {status})')
    if status is not None:
        return LlmError('unavailable', f'the model service failed (status {status})')
    return LlmError('unavailable', 'the model service could not be reached')


# --- the client -------------------------------------------------------------------------------------------------------------------------

class GeminiClient:
    """LlmClient backed by Gemini. `model` is the configured ANALYST_MODEL; `transport` has one
    method, generate(model, contents, config), returning a google-genai response."""

    def __init__(self, *, model, transport):
        from google.genai import types
        if not isinstance(model, str) or not model:
            raise ValueError('a model name is required')
        self._types, self._model, self._transport = types, model, transport

    def __repr__(self):
        return f'GeminiClient(model={self._model!r})'

    def generate(self, request: LlmRequest) -> LlmResponse:
        types = self._types
        try:
            config = types.GenerateContentConfig(
                system_instruction=request.system, temperature=TEMPERATURE, candidate_count=1,
                max_output_tokens=request.max_output_tokens,
                tools=[types.Tool(function_declarations=_declarations(types, request.tools))] if request.tools else None,
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode='AUTO'))
                if request.tools else None,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                http_options=types.HttpOptions(timeout=max(1, int(request.timeout_s * 1000)),
                                               retry_options=types.HttpRetryOptions(attempts=1)))
            contents = _contents(types, request.messages)
        except LlmError:
            raise
        except Exception:
            raise LlmError('bad_response', 'the request could not be built') from None
        try:
            response = self._transport.generate(self._model, contents, config)
        except Exception as exc:
            error = map_exception(exc)
            log.warning('analyst_gemini_error', extra={'fields': {
                'kind': error.kind, 'status': _status(exc), 'exception': type(exc).__name__}})
            raise error from None
        try:
            return _parse(response)
        except LlmError:
            raise
        except Exception:                      # a reply of an unexpected shape: no detail, no cause
            raise LlmError('bad_response', 'the reply could not be read') from None


def build_client(settings):
    """The Gemini client for these settings, or None when the analyst is not ready or the SDK is
    not installed (the route then answers 503). The key is read through Settings, which takes it
    from the process environment only. Constructing the client makes no request."""
    if not settings.analyst_ready:
        return None
    if not sdk_available():
        log.warning('analyst_sdk_missing')
        return None
    return GeminiClient(model=settings.analyst_model, transport=SdkTransport(settings.analyst_credential()))
