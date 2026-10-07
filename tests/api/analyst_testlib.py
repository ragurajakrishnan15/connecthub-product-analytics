"""Test doubles for the analyst chat engine: a scripted fake LLM and a fake database engine.

ScriptedLlm is deterministic: it plays a fixed script, one entry per generate() call, records every
request it receives and fails loudly if it is called more often than scripted. It never contacts
the network, holds no credential and has no randomness."""
import json
from types import SimpleNamespace

from api.analyst.llm import LlmResponse, ToolCall, Usage


class ScriptedLlm:
    """Plays `script` in order. An entry is an LlmResponse, an exception to raise, or a function
    (request) -> LlmResponse | exception for scripts that must react to the request."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def generate(self, request):
        self.requests.append(request)
        if not self.script:
            raise AssertionError('the fake LLM was called more often than the test scripted')
        step = self.script.pop(0)
        if callable(step):
            step = step(request)
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def calls(self):
        return len(self.requests)


def say(text, tokens=(10, 5)):
    return LlmResponse(text=text, usage=Usage(*tokens))


def use(*calls, tokens=(10, 5)):
    return LlmResponse(tool_calls=tuple(calls), usage=Usage(*tokens))


def call(name, **arguments):
    return ToolCall(name, arguments)


# --- a fake database engine (records what is run on its connections) ---------------------------------

class FakeTransaction:
    def __init__(self, log):
        self.log = log

    def commit(self):
        self.log.append('commit')

    def rollback(self):
        self.log.append('rollback')


class FakeConnection:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append('close')

    def begin(self):
        self.log.append('begin')
        return FakeTransaction(self.log)

    def execute(self, statement, *args, **kwargs):
        self.log.append(f'execute: {statement}')


class FakeEngine:
    def __init__(self):
        self.log = []

    def connect(self):
        return FakeConnection(self.log)

    @property
    def connections(self):
        return self.log.count('begin')


def envelope(data, **meta):
    payload = {'data': data, 'meta': {
        'as_of': '2025-12-31', 'data_start': '2025-01-02', 'data_end': '2025-12-31',
        'effective_range': None, 'filters': {}, 'sources': ['gold.fake_table'],
        'data_version': 'v-test', 'generated_at': '2026-01-01T00:00:00Z',
        'definitions': 'docs/metric-definitions.md', 'caveats': [], **meta}}
    return SimpleNamespace(model_dump=lambda mode='json': json.loads(json.dumps(payload)))


class Service:
    """Stands in for one service function: records calls, returns or raises what it was given."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else envelope({'value': 1})

    def __call__(self, *args):
        self.calls.append(args)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeClock:
    """A clock the test moves by hand, so timeouts are exact."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
