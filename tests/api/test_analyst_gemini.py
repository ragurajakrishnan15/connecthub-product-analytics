"""The Gemini provider (PHASE_6_PLAN.md step 7), tested with a fake transport only.

Nothing here contacts Google: the transport is a fake that returns real google-genai response
objects (so the request and reply handling is checked against the SDK's own types), sockets are
blocked, and the only key is a made-up value. The real transport is never constructed except with
the SDK client replaced by a recorder."""
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
from analyst_testlib import FakeEngine, Service, envelope
from api_testlib import capture_logs, clean_env, make_settings  # noqa: F401
from fastapi.testclient import TestClient
from google.genai import errors, types

import api.analyst.gemini as gemini
from api.analyst import grounding, tools
from api.analyst.engine import Budget, run_chat
from api.analyst.gemini import GeminiClient, build_client, map_exception
from api.analyst.llm import LlmError, LlmRequest, ToolCall
from api.main import create_app
from pipeline import config

pytestmark = pytest.mark.usefixtures('clean_env')
ROOT = Path(config.PROJECT_ROOT)
FAKE_KEY = 'fake-gemini-key-for-tests-0123456789'
OVERVIEW = {'mrr': {'value': 61870.0, 'previous': 58200.0}, 'activation': {'rate': 0.639}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing may leave this machine. Loopback stays open for the in-process test client's event loop."""
    real_connect = socket.socket.connect

    def refuse(*args, **kwargs):
        raise AssertionError('a test tried to use the network')

    def guarded(self, address, *args, **kwargs):
        if isinstance(address, tuple) and address and address[0] in ('127.0.0.1', '::1', 'localhost'):
            return real_connect(self, address, *args, **kwargs)
        return refuse()
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)


# --- fakes -----------------------------------------------------------------------------------------------------------

def reply(*parts, finish='STOP', usage=(10, 5), thoughts=None, block=None):
    body = {'candidates': [{'content': {'role': 'model', 'parts': list(parts)}, 'finish_reason': finish}]}
    if usage is not None:
        body['usage_metadata'] = {'prompt_token_count': usage[0], 'candidates_token_count': usage[1],
                                  **({'thoughts_token_count': thoughts} if thoughts else {})}
    if block:
        body['prompt_feedback'] = {'block_reason': block}
    return types.GenerateContentResponse.model_validate(body)


def say(text, **kw):
    return reply({'text': text}, **kw)


def ask_for(*calls, **kw):
    return reply(*[{'function_call': {'name': n, 'args': a}, **({'thought_signature': s} if s else {})}
                   for n, a, s in [(c + (None,))[:3] for c in calls]], **kw)


class FakeTransport:
    """Plays a script of replies or exceptions; records what it was asked. Never touches the network."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def generate(self, model, contents, config_):
        self.calls.append((model, contents, config_))
        if not self.script:
            raise AssertionError('the fake transport was called more often than scripted')
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def client_with(*script, model='fake-model'):
    transport = FakeTransport(*script)
    return GeminiClient(model=model, transport=transport), transport


def request(messages=({'role': 'user', 'text': 'What is MRR?'},), *, tools_=None, **kw):
    return LlmRequest(system=kw.pop('system', 'SYSTEM TEXT'), messages=tuple(messages),
                      tools=tuple(tools.declarations() if tools_ is None else tools_),
                      max_output_tokens=kw.pop('max_output_tokens', 512), timeout_s=kw.pop('timeout_s', 20.0))


def stub(monkeypatch, tool_name, data=None):
    module, function = tools.TOOLS[tool_name].service.rsplit('.', 1)
    service = Service(envelope(OVERVIEW if data is None else data))
    monkeypatch.setattr(sys.modules[module], function, service)
    return service


# --- 1. the request Gemini receives --------------------------------------------------------------------------------------

def test_the_config_is_explicit_bounded_and_has_no_automatic_function_calling():
    client, transport = client_with(say('ok'))
    client.generate(request(max_output_tokens=777, timeout_s=12.5))
    model, contents, cfg = transport.calls[0]
    assert model == 'fake-model'
    assert cfg.system_instruction == 'SYSTEM TEXT'
    assert cfg.temperature == 0.0 and cfg.candidate_count == 1 and cfg.max_output_tokens == 777
    assert cfg.automatic_function_calling.disable is True
    assert cfg.http_options.timeout == 12500
    assert cfg.http_options.retry_options.attempts == 1             # one generate() is one HTTP request
    assert cfg.tool_config.function_calling_config.mode.name == 'AUTO'


def test_only_the_approved_tools_are_declared_and_their_schemas_are_valid_gemini_schemas():
    client, transport = client_with(say('ok'))
    client.generate(request())
    declared = transport.calls[0][2].tools
    assert len(declared) == 1
    functions = declared[0].function_declarations
    assert [f.name for f in functions] == list(tools.TOOL_NAMES)
    by_name = {f.name: f for f in functions}
    assert by_name['get_overview'].parameters is None and by_name['get_meta'].parameters is None   # no empty OBJECT schema
    workspaces = by_name['list_workspaces'].parameters
    assert workspaces.type.name == 'OBJECT'
    assert workspaces.properties['tier'].enum == ['Critical', 'At Risk', 'Healthy', 'Champion']
    limit = workspaces.properties['limit']
    assert (limit.type.name, limit.minimum, limit.maximum) == ('INTEGER', 1, 100)
    assert by_name['get_experiment'].parameters.required == ['experiment_id']
    assert by_name['get_experiment'].parameters.properties['experiment_id'].pattern == '^[a-z0-9_]{1,64}$'
    activation = by_name['get_activation'].parameters.properties
    assert activation['start'].format is None and activation['granularity'].default is None   # unsupported keywords dropped
    assert 'YYYY-MM-DD' in activation['start'].description
    assert by_name['get_feature_adoption'].parameters.properties['features'].type.name == 'ARRAY'


def test_without_tools_none_are_sent_so_the_model_cannot_ask_for_one():
    client, transport = client_with(say('ok'))
    client.generate(request(tools_=()))
    cfg = transport.calls[0][2]
    assert cfg.tools is None and cfg.tool_config is None


def test_a_transcript_becomes_user_model_and_function_response_contents():
    client, transport = client_with(say('ok'))
    messages = [{'role': 'user', 'text': 'q1'}, {'role': 'assistant', 'text': 'a1'},
                {'role': 'user', 'text': 'q2'},
                {'role': 'assistant', 'text': None, 'tool_calls': [
                    {'id': 'call-1', 'name': 'get_overview', 'arguments': {}},
                    {'id': 'call-2', 'name': 'get_nps', 'arguments': {'min_responses': 20}}]},
                {'role': 'tool', 'results': [{'id': 'call-1', 'name': 'get_overview', 'content': json.dumps({'ok': True, 'data': {'x': 1}})},
                                              {'id': 'call-2', 'name': 'get_nps', 'content': 'not json'}]}]
    client.generate(request(messages))
    contents = transport.calls[0][1]
    assert [c.role for c in contents] == ['user', 'model', 'user', 'model', 'user']
    assert [c.parts[0].text for c in contents[:3]] == ['q1', 'a1', 'q2']
    calls = contents[3].parts
    assert [(p.function_call.name, dict(p.function_call.args)) for p in calls] == [('get_overview', {}), ('get_nps', {'min_responses': 20})]
    responses = contents[4].parts
    assert [p.function_response.name for p in responses] == ['get_overview', 'get_nps']
    assert responses[0].function_response.response == {'ok': True, 'data': {'x': 1}}
    assert responses[1].function_response.response == {'output': 'not json'}      # still a dict, still data


@pytest.mark.parametrize('message', [{'role': 'system', 'text': 'x'}, {'role': 'tool', 'results': 'x'},
                                      {'role': 'developer', 'text': 'x'}, {'role': 'user'}, {'role': 'user', 'text': 5}])
def test_a_message_that_is_not_an_engine_shape_is_never_sent(message):
    client, transport = client_with(say('ok'))
    with pytest.raises(LlmError) as info:
        client.generate(request([message]))
    assert info.value.kind == 'bad_response' and transport.calls == []


def test_only_what_the_engine_built_reaches_the_model():
    client, transport = client_with(say('ok'))
    client.generate(request([{'role': 'user', 'text': 'hello'}], system='server instructions'))
    model, contents, cfg = transport.calls[0]
    blob = json.dumps([c.model_dump(mode='json', exclude_none=True) for c in contents]) + str(cfg.system_instruction)
    assert 'hello' in blob and 'server instructions' in blob and FAKE_KEY not in blob


# --- 2. the reply ---------------------------------------------------------------------------------------------------------------

def test_a_text_reply_and_its_usage():
    client, _ = client_with(say('MRR is $61,870.', usage=(40, 12), thoughts=3))
    out = client.generate(request())
    assert out.text == 'MRR is $61,870.' and out.tool_calls == ()
    assert (out.usage.input_tokens, out.usage.output_tokens) == (40, 15)          # thinking counts as output
    no_usage, _ = client_with(say('x', usage=None))
    assert no_usage.generate(request()).usage is None                             # the engine then estimates


def test_thought_parts_are_not_part_of_the_answer():
    client, _ = client_with(reply({'text': 'private reasoning', 'thought': True}, {'text': 'The answer.'}))
    assert client.generate(request()).text == 'The answer.'


def test_tool_requests_come_back_with_their_arguments_and_thought_signatures():
    client, _ = client_with(ask_for(('get_overview', {}, b'sig-1'), ('get_nps', {'min_responses': 30}, None)))
    out = client.generate(request())
    assert out.text is None
    assert [(c.name, c.arguments) for c in out.tool_calls] == [('get_overview', {}), ('get_nps', {'min_responses': 30})]
    assert out.tool_calls[0].provider_state is not None and out.tool_calls[1].provider_state is None
    assert 'sig' not in repr(out.tool_calls[0])


def test_a_thought_signature_is_returned_untouched_on_the_next_request():
    client, transport = client_with(ask_for(('get_overview', {}, b'\x00\x01signature-bytes')), say('done'))
    first = client.generate(request())
    call = first.tool_calls[0]
    messages = [{'role': 'user', 'text': 'q'},
                {'role': 'assistant', 'text': None, 'tool_calls': [{'id': 'call-1', 'name': call.name, 'arguments': call.arguments,
                                                                      'provider_state': call.provider_state}]},
                {'role': 'tool', 'results': [{'id': 'call-1', 'name': call.name, 'content': '{"ok": true}'}]}]
    client.generate(request(messages))
    part = transport.calls[1][1][1].parts[0]
    assert part.thought_signature == b'\x00\x01signature-bytes' and part.function_call.name == 'get_overview'
    messages[1]['tool_calls'][0]['provider_state'] = '%%not base64%%'              # a bad value is ignored, not trusted
    client.script = None
    transport.script.append(say('x'))
    client.generate(request(messages))
    assert transport.calls[2][1][1].parts[0].thought_signature is None


def test_a_tool_request_without_arguments_has_empty_arguments():
    client, _ = client_with(reply({'function_call': {'name': 'get_overview'}}))
    assert client.generate(request()).tool_calls[0].arguments == {}


@pytest.mark.parametrize('response, kind', [
    (types.GenerateContentResponse(), 'bad_response'),
    (types.GenerateContentResponse(candidates=[]), 'bad_response'),
    (say('', finish='SAFETY'), 'refused'),
    (reply(finish='RECITATION'), 'refused'),
    (reply(finish='MALFORMED_FUNCTION_CALL'), 'bad_response'),
    (reply(finish='UNEXPECTED_TOOL_CALL'), 'bad_response'),
    (say('x', block='SAFETY'), 'refused'),
    (reply(*[{'function_call': {'name': 'get_nps', 'args': {}}}] * 17), 'bad_response'),
])
def test_unusable_replies_are_safe_errors(response, kind):
    client, _ = client_with(response)
    with pytest.raises(LlmError) as info:
        client.generate(request())
    assert info.value.kind == kind


def test_an_empty_reply_is_an_empty_response_for_the_engine_to_report():
    client, _ = client_with(reply(), reply({'text': '   '}))
    assert client.generate(request()).text is None and client.generate(request()).text is None


# --- 3. failures -------------------------------------------------------------------------------------------------------------------

SECRET_MESSAGE = f'boom {FAKE_KEY} Bearer abc password=hunter2 SELECT * FROM gold.x'


@pytest.mark.parametrize('exc, kind', [
    (TimeoutError(SECRET_MESSAGE), 'timeout'),
    (httpx.ReadTimeout(SECRET_MESSAGE), 'timeout'),
    (httpx.ConnectTimeout(SECRET_MESSAGE), 'timeout'),
    (errors.ClientError(408, {'error': {'message': SECRET_MESSAGE}}), 'timeout'),
    (errors.ServerError(504, {'error': {'message': SECRET_MESSAGE}}), 'timeout'),
    (errors.ClientError(401, {'error': {'message': SECRET_MESSAGE}}), 'auth'),
    (errors.ClientError(403, {'error': {'message': SECRET_MESSAGE}}), 'auth'),
    (errors.ClientError(429, {'error': {'message': SECRET_MESSAGE}}), 'rate_limited'),
    (errors.ClientError(400, {'error': {'message': SECRET_MESSAGE}}), 'bad_response'),
    (errors.ClientError(404, {'error': {'message': SECRET_MESSAGE}}), 'bad_response'),
    (errors.ServerError(500, {'error': {'message': SECRET_MESSAGE}}), 'unavailable'),
    (errors.ServerError(503, {'error': {'message': SECRET_MESSAGE}}), 'unavailable'),
    (httpx.ConnectError(SECRET_MESSAGE), 'unavailable'),
    (ConnectionResetError(SECRET_MESSAGE), 'unavailable'),
    (RuntimeError(SECRET_MESSAGE), 'unavailable'),
    (ValueError(SECRET_MESSAGE), 'unavailable'),
])
def test_every_failure_is_mapped_to_a_fixed_safe_error(exc, kind):
    client, _ = client_with(exc)
    with pytest.raises(LlmError) as info:
        client.generate(request())
    error = info.value
    assert error.kind == kind
    for leaked in (FAKE_KEY, 'Bearer', 'hunter2', 'SELECT', 'gold.x', 'boom'):
        assert leaked not in str(error) and leaked not in repr(error.args)
    assert error.__cause__ is None and error.__suppress_context__                   # no chained exception to print


def test_the_failure_log_line_has_the_kind_and_status_but_no_exception_text(monkeypatch):
    lines = capture_logs()
    client, _ = client_with(errors.ClientError(401, {'error': {'message': SECRET_MESSAGE}}))
    with pytest.raises(LlmError):
        client.generate(request())
    logged = json.dumps(lines())
    assert 'analyst_gemini_error' in logged and '"auth"' in logged and '401' in logged
    assert FAKE_KEY not in logged and 'hunter2' not in logged and 'SELECT' not in logged


def test_map_exception_passes_an_llm_error_through_and_knows_nothing_else():
    original = LlmError('refused', 'x')
    assert map_exception(original) is original
    assert map_exception(object()).kind == 'unavailable'


# --- 4. through the chat engine --------------------------------------------------------------------------------------------------------

def chat(client, question='What is MRR?', **options):
    settings = options.pop('settings', None) or make_settings()
    return run_chat([{'role': 'user', 'content': question}], llm=client, engine=FakeEngine(), settings=settings, **options)


def test_a_model_that_asks_for_a_tool_gets_its_result_and_answers(monkeypatch):
    service = stub(monkeypatch, 'get_overview')
    client, transport = client_with(ask_for(('get_overview', {}), usage=(30, 4)), say('MRR is $61,870.', usage=(80, 9)))
    result = chat(client)
    assert result.status == 'answered' and result.answer == 'MRR is $61,870.'
    assert len(service.calls) == 1 and len(transport.calls) == 2
    second = transport.calls[1][1]
    assert [c.role for c in second] == ['user', 'model', 'user']
    returned = second[2].parts[0].function_response
    assert returned.name == 'get_overview' and returned.response['ok'] is True and returned.response['data'] == OVERVIEW
    assert returned.response['source']['endpoint'] == '/api/overview'                   # the engine's own result, unchanged
    assert result.usage == {'requests': 2, 'input_tokens': 110, 'output_tokens': 13, 'total_tokens': 123}


def test_several_tool_calls_in_one_turn_and_across_turns_stay_inside_the_limit(monkeypatch):
    overview, nps = stub(monkeypatch, 'get_overview'), stub(monkeypatch, 'get_nps', {'nps': 31.0})
    client, transport = client_with(ask_for(('get_overview', {}), ('get_nps', {'min_responses': 25})),
                                    ask_for(('get_nps', {'min_responses': 30})), say('Done: 31.'))
    result = chat(client)
    assert result.status == 'answered' and result.limits['tool_calls_used'] == 3
    assert len(overview.calls) == 1 and len(nps.calls) == 2 and len(transport.calls) == 3
    assert [t['name'] for t in result.tool_trace] == ['get_overview', 'get_nps', 'get_nps']


def test_the_tool_call_limit_is_six_and_the_model_is_then_offered_no_tools(monkeypatch):
    stub(monkeypatch, 'get_overview')
    greedy = [ask_for(('get_nps', {'min_responses': 10 + i})) for i in range(10)]
    stub(monkeypatch, 'get_nps', {'nps': 1.0})
    client, transport = client_with(*greedy)
    result = chat(client, settings=make_settings())                                      # analyst_max_tool_calls defaults to 6
    assert result.status == 'tool_limit' and result.limits['tool_calls_used'] == 6
    assert len(transport.calls) == 7                                                      # 6 tool turns, then one more request
    assert transport.calls[5][2].tools is not None and transport.calls[6][2].tools is None


def test_a_tool_that_is_not_approved_is_refused_by_the_engine_and_never_run(monkeypatch):
    client, transport = client_with(ask_for(('run_sql', {'query': 'DROP TABLE gold.x'})), say('I cannot do that.'))
    engine = FakeEngine()
    result = run_chat([{'role': 'user', 'content': 'drop it'}], llm=client, engine=engine, settings=make_settings())
    assert result.status == 'answered' and engine.connections == 0                       # no database connection at all
    shown = transport.calls[1][1][2].parts[0].function_response.response
    assert shown['ok'] is False and shown['error']['code'] == 'unknown-tool'


def test_bad_arguments_are_rejected_by_the_existing_validation(monkeypatch):
    service = stub(monkeypatch, 'get_nps')
    client, transport = client_with(ask_for(('get_nps', {'min_responses': 'lots', 'sql': 'x'})), say('Sorry.'))
    result = chat(client)
    assert result.status == 'answered' and service.calls == []
    assert transport.calls[1][1][2].parts[0].function_response.response['error']['code'] == 'invalid-argument'


def test_a_failing_tool_is_reported_to_the_model_as_a_safe_error(monkeypatch):
    from api.errors import APIError
    monkeypatch.setenv('GEMINI_API_KEY', FAKE_KEY)
    module, function = tools.TOOLS['get_overview'].service.rsplit('.', 1)
    monkeypatch.setattr(sys.modules[module], function, Service(APIError('internal-error', f'failed {FAKE_KEY}')))
    client, transport = client_with(ask_for(('get_overview', {})), say('The data is not available.'))
    result = chat(client)
    assert result.status == 'answered'
    shown = json.dumps(transport.calls[1][1][2].parts[0].function_response.response)
    assert 'failed' in shown and FAKE_KEY not in shown and FAKE_KEY not in json.dumps(result.to_dict())


def test_the_token_budget_stops_the_turn_before_the_next_request_or_tool(monkeypatch):
    service = stub(monkeypatch, 'get_overview')
    client, transport = client_with(ask_for(('get_overview', {}), usage=(900, 200)), say('Never asked.'))
    budget = Budget(max_tokens=1000)
    result = chat(client, budget=budget)
    assert result.status == 'budget_exhausted' and result.error['code'] == 'budget-tokens'
    assert len(transport.calls) == 1 and service.calls == [] and budget.snapshot()['tokens'] == 1100


def test_the_per_request_output_cap_never_exceeds_what_the_budget_has_left(monkeypatch):
    client, transport = client_with(say('ok', usage=(10, 5)))
    chat(client, budget=Budget(max_tokens=300), settings=make_settings(analyst_max_output_tokens=1024))
    assert transport.calls[0][2].max_output_tokens == 300


@pytest.mark.parametrize('exc, status, code', [
    (TimeoutError('slow'), 'timeout', 'upstream-timeout'),
    (errors.ClientError(401, {}), 'upstream_error', 'upstream-not-authorized'),
    (errors.ClientError(429, {}), 'upstream_error', 'upstream-rate-limited'),
    (errors.ServerError(503, {}), 'upstream_error', 'upstream-error'),
    (RuntimeError(SECRET_MESSAGE), 'upstream_error', 'upstream-error'),
])
def test_provider_failures_become_coded_results_with_fixed_messages(exc, status, code):
    client, _ = client_with(exc)
    result = chat(client)
    assert (result.status, result.error['code']) == (status, code)
    assert FAKE_KEY not in json.dumps(result.to_dict()) and 'boom' not in json.dumps(result.to_dict())


@pytest.mark.parametrize('response, status', [
    (reply(), 'empty_response'), (say('x', finish='SAFETY', block='SAFETY'), 'upstream_error'),
    (reply(finish='MALFORMED_FUNCTION_CALL'), 'upstream_error'), (types.GenerateContentResponse(), 'upstream_error')])
def test_malformed_replies_end_the_turn_safely(response, status):
    client, _ = client_with(response)
    assert chat(client).status == status


def test_the_whole_chain_is_grounded_and_withholds_an_unsupported_number(monkeypatch):
    stub(monkeypatch, 'get_overview')
    client, _ = client_with(ask_for(('get_overview', {})), say('MRR is $99,999.'))
    out = grounding.run_grounded_chat([{'role': 'user', 'content': 'MRR?'}], llm=client, engine=FakeEngine(),
                                      settings=make_settings())
    assert out.result.status == 'ungrounded' and out.result.answer is None
    client, _ = client_with(ask_for(('get_overview', {})), say('MRR is $61,870.'))
    ok = grounding.run_grounded_chat([{'role': 'user', 'content': 'MRR?'}], llm=client, engine=FakeEngine(),
                                     settings=make_settings())
    assert ok.result.status == 'answered' and ok.report.supporting_sources == ['call-1']


def test_the_provider_state_reaches_the_model_but_never_the_trace_or_the_result(monkeypatch):
    stub(monkeypatch, 'get_overview')
    client, transport = client_with(ask_for(('get_overview', {}, b'opaque-signature')), say('Fine.'))
    result = chat(client)
    assert 'provider_state' not in json.dumps(result.to_dict()) and 'opaque' not in json.dumps(result.to_dict())
    assert transport.calls[1][1][1].parts[0].thought_signature == b'opaque-signature'


# --- 5. configuration and the key ----------------------------------------------------------------------------------------------------------

def ready(**overrides):
    return make_settings(analyst_enabled=True, gemini_api_key=FAKE_KEY, analyst_model='fake-model', **overrides)


def test_nothing_is_built_unless_the_analyst_is_enabled_keyed_and_has_a_model():
    for settings in (make_settings(), make_settings(analyst_enabled=True), make_settings(analyst_enabled=True, gemini_api_key=FAKE_KEY),
                     make_settings(analyst_enabled=True, analyst_model='m'),
                     make_settings(gemini_api_key=FAKE_KEY, analyst_model='m')):
        assert build_client(settings) is None


def test_the_key_comes_from_the_process_environment_through_settings(monkeypatch):
    from api.settings import Settings
    seen = {}

    class RecordingClient:
        def __init__(self, api_key=None, **kwargs):
            seen['key'], seen['kwargs'] = api_key, kwargs
    monkeypatch.setattr('google.genai.Client', RecordingClient)
    monkeypatch.setenv('API_DB_PASSWORD', 'not-a-real-password-123')
    monkeypatch.setenv('ANALYST_ENABLED', 'true')
    monkeypatch.setenv('ANALYST_MODEL', 'fake-model')
    monkeypatch.setenv('GEMINI_API_KEY', FAKE_KEY)
    client = build_client(Settings())
    assert isinstance(client, GeminiClient) and seen['key'] == FAKE_KEY and seen['kwargs'] == {}
    assert FAKE_KEY not in repr(client) and FAKE_KEY not in repr(client._transport) and FAKE_KEY not in str(Settings().summary())
    monkeypatch.delenv('GEMINI_API_KEY')
    assert build_client(Settings()) is None                                                 # no key, no client


def test_a_dotenv_file_is_not_a_source_of_the_key(monkeypatch, tmp_path):
    from api.settings import Settings
    (tmp_path / '.env').write_text(f'GEMINI_API_KEY={FAKE_KEY}\nANALYST_ENABLED=true\nANALYST_MODEL=m\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('API_DB_PASSWORD', 'not-a-real-password-123')
    settings = Settings()
    assert settings.analyst_credential() is None and build_client(settings) is None


def test_without_the_sdk_there_is_no_client_and_the_route_answers_503(monkeypatch):
    monkeypatch.setattr(gemini, 'sdk_available', lambda: False)
    assert build_client(ready()) is None
    app = create_app(ready())
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post('/api/analyst/chat', json={'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 503 and r.json()['type'].endswith('analyst-not-configured')


def test_the_app_builds_the_gemini_client_when_configured_and_makes_no_request_doing_so():
    app = create_app(ready())                       # sockets are blocked: any request would fail this test
    assert isinstance(app.state.analyst_llm, GeminiClient)
    assert create_app(make_settings()).state.analyst_llm is None
    scripted = object()
    assert create_app(ready(), analyst_llm=scripted).state.analyst_llm is scripted


def test_a_model_name_is_required():
    with pytest.raises(ValueError):
        GeminiClient(model='', transport=FakeTransport())


# --- 6. the key never leaves ---------------------------------------------------------------------------------------------------------------

def test_through_the_route_a_provider_failure_leaks_nothing(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', FAKE_KEY)
    transport = FakeTransport(errors.ClientError(401, {'error': {'message': SECRET_MESSAGE}}),
                              RuntimeError(SECRET_MESSAGE), say(f'The key is {FAKE_KEY}.'))
    app = create_app(ready(), analyst_llm=GeminiClient(model='fake-model', transport=transport))
    with TestClient(app, raise_server_exceptions=False) as client:
        lines = capture_logs()
        app.state.engine, real = FakeEngine(), app.state.engine
        responses = [client.post('/api/analyst/chat', json={'messages': [{'role': 'user', 'content': f'q{i}'}]}) for i in range(3)]
        app.state.engine = real
    assert [r.status_code for r in responses[:2]] == [502, 502]
    for r in responses:
        assert FAKE_KEY not in r.text and FAKE_KEY not in str(dict(r.headers)) and 'hunter2' not in r.text
    assert FAKE_KEY not in json.dumps(lines()) and 'hunter2' not in json.dumps(lines())


def test_the_provider_source_holds_no_key_no_environment_read_and_no_print():
    source = (ROOT / 'api' / 'analyst' / 'gemini.py').read_text(encoding='utf-8')
    for forbidden in ('os.environ', 'getenv', 'print(', 'AIza', 'GEMINI_API_KEY', 'gemini_api_key', 'dotenv', 'subprocess'):
        assert forbidden not in source, forbidden
    assert 'analyst_credential' in source


def test_only_the_settings_module_reads_the_key_and_only_the_factory_asks_for_it():
    users = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / 'api').rglob('*.py')
                   if 'analyst_credential' in p.read_text(encoding='utf-8'))
    assert users == ['api/analyst/gemini.py', 'api/settings.py']


def test_the_key_is_in_no_frontend_asset_or_tracked_file():
    for name in ('index.html',):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert FAKE_KEY not in text and 'gemini' not in text.lower()
    assert not [p for p in (ROOT / 'tests' / 'dashboard').glob('*.mjs') if FAKE_KEY in p.read_text(encoding='utf-8')]


def test_the_sdk_version_is_the_pinned_one():
    import google.genai
    pin = (ROOT / 'requirements' / 'analyst.txt').read_text(encoding='utf-8')
    assert f'google-genai=={google.genai.__version__}' in pin


def test_the_fake_provider_is_deterministic():
    outputs = []
    for _ in range(3):
        client, transport = client_with(ask_for(('get_overview', {}), ('get_nps', {'min_responses': 12}), usage=(7, 3)))
        out = client.generate(request())
        cfg = transport.calls[0][2]
        outputs.append((out.tool_calls, out.usage, cfg.temperature, cfg.max_output_tokens,
                        [f.name for f in cfg.tools[0].function_declarations]))
    assert outputs[0] == outputs[1] == outputs[2]
    assert isinstance(ToolCall('a', {}), ToolCall) and ToolCall('a', {}, 'x') == ToolCall('a', {}, 'y')   # state is not identity


# --- 7. mutation tests: break the provider, the table must notice ------------------------------------------------------------------------------------------

def scenarios():
    """(name, passed) for each guarantee, run against the current code."""
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))
    try:
        client, transport = client_with(say('ok'))
        client.generate(request())
        cfg = transport.calls[0][2]
        check('no automatic function calling', cfg.automatic_function_calling.disable is True)
        check('no sdk retries', cfg.http_options.retry_options.attempts == 1)
        check('timeout from the request', cfg.http_options.timeout == 20000)
        check('output cap from the request', cfg.max_output_tokens == 512)
        check('only approved tools declared', [f.name for f in cfg.tools[0].function_declarations] == list(tools.TOOL_NAMES))
        client, transport = client_with(say('ok'))
        client.generate(request(tools_=()))
        check('no tools when none are offered', transport.calls[0][2].tools is None)
        client, transport = client_with(say('ok'))
        try:
            client.generate(request([{'role': 'system', 'text': 'x'}]))
            check('foreign roles are refused', False)
        except LlmError:
            check('foreign roles are refused', not transport.calls)
        for exc, kind in ((errors.ClientError(401, {'e': FAKE_KEY}), 'auth'), (errors.ClientError(429, {}), 'rate_limited'),
                          (TimeoutError(FAKE_KEY), 'timeout'), (RuntimeError(FAKE_KEY), 'unavailable')):
            client, _ = client_with(exc)
            try:
                client.generate(request())
                check(f'{kind} is an error', False)
            except LlmError as error:
                check(f'{kind} maps and does not leak', error.kind == kind and FAKE_KEY not in str(error) and error.__cause__ is None)
        client, _ = client_with(reply(finish='MALFORMED_FUNCTION_CALL'))
        try:
            client.generate(request())
            check('malformed call is an error', False)
        except LlmError:
            check('malformed call is an error', True)
        client, _ = client_with(say('partial text', finish='SAFETY'), reply({'text': 'secret thinking', 'thought': True}, {'text': 'Answer'}))
        try:
            client.generate(request())
            check('a filtered reply is not an answer, even with text', False)
        except LlmError as error:
            check('a filtered reply is not an answer, even with text', error.kind == 'refused')
        check('thought text is dropped', client.generate(request()).text == 'Answer')
        client, _ = client_with(ask_for(('get_overview', {}, b'sig')))
        check('thought signature kept as state', client.generate(request()).tool_calls[0].provider_state is not None)
        client, _ = client_with(say('x', usage=(40, 12), thoughts=3))
        check('thinking tokens are output tokens', client.generate(request()).usage.output_tokens == 15)
    except Exception as exc:                               # a mutation that breaks everything is detected too
        check(f'scenarios ran ({type(exc).__name__})', False)
    return [name for name, ok in results if not ok]


def test_the_scenario_table_passes_on_the_real_provider():
    assert scenarios() == []


def _leaky(exc):
    return LlmError('unavailable', f'failed: {exc!r}')


MUTATIONS = {
    'sdk retries are left on': ('types.HttpRetryOptions', lambda mp: mp.setattr(types, 'HttpRetryOptions', lambda attempts: types.HttpRetryOptions.model_construct(attempts=5))),
    'automatic function calling is on': ('types.AutomaticFunctionCallingConfig', lambda mp: mp.setattr(
        types, 'AutomaticFunctionCallingConfig', lambda disable: types.AutomaticFunctionCallingConfig.model_construct(disable=False))),
    'the request timeout is ignored': ('request', lambda mp: mp.setattr(types, 'HttpOptions', lambda timeout, retry_options: types.HttpOptions(timeout=1, retry_options=retry_options))),
    'an unapproved tool is declared': ('tools', lambda mp: mp.setattr(gemini, '_declarations', lambda t, ts: [
        types.FunctionDeclaration(name=n) for n in (*tools.TOOL_NAMES, 'run_sql')])),
    'foreign roles are sent': ('contents', lambda mp: mp.setattr(gemini, '_contents', lambda t, ms: [t.Content(role='user', parts=[t.Part(text=str(m))]) for m in ms])),
    'exception text is kept': ('map', lambda mp: mp.setattr(gemini, 'map_exception', _leaky)),
    'errors are not mapped': ('map', lambda mp: mp.setattr(gemini, 'map_exception', lambda exc: LlmError('unavailable', 'x'))),
    'thought text is read as the answer': ('parse', lambda mp: mp.setattr(gemini, '_is_thought', lambda part: False)),
}


@pytest.mark.parametrize('name', sorted(MUTATIONS))
def test_mutation_is_detected(name, monkeypatch):
    MUTATIONS[name][1](monkeypatch)
    assert scenarios() != [], f'mutation {name!r} was not detected'


def test_mutation_the_thought_signature_is_dropped(monkeypatch):
    original = gemini._parse

    def dropping(response):
        out = original(response)
        return out.__class__(text=out.text, tool_calls=tuple(ToolCall(c.name, c.arguments) for c in out.tool_calls), usage=out.usage)
    monkeypatch.setattr(gemini, '_parse', dropping)
    assert 'thought signature kept as state' in scenarios()


def test_mutation_thinking_tokens_are_not_counted(monkeypatch):
    monkeypatch.setattr(gemini, '_usage', lambda m: gemini.Usage(m.prompt_token_count or 0, m.candidates_token_count or 0))
    assert 'thinking tokens are output tokens' in scenarios()


def test_mutation_blocked_replies_are_returned_as_answers(monkeypatch):
    real = gemini._REFUSED
    monkeypatch.setattr(gemini, '_REFUSED', set())
    client, _ = client_with(say('partial', finish='SAFETY'))
    assert client.generate(request()).text == 'partial'   # without the refusal check a filtered reply is an answer
    monkeypatch.setattr(gemini, '_REFUSED', real)         # (restored explicitly: a blanket undo would drop the Google guard)
    client, _ = client_with(say('partial', finish='SAFETY'))
    with pytest.raises(LlmError):
        client.generate(request())


def test_the_provider_has_no_way_to_read_a_file_or_the_environment():
    import ast
    tree = ast.parse((ROOT / 'api' / 'analyst' / 'gemini.py').read_text(encoding='utf-8'))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {'open', 'environ', 'getenv', 'read_text', 'read_bytes', 'Path', 'os', 'dotenv', 'subprocess'}
    imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [ast.alias(name=n.module or '')])}
    assert imported <= {'base64', 'binascii', 'json', 'logging', 'api', 'google', 'httpx'}

