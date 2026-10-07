"""The chat engine with the real tools on the real local warehouse, driven by a scripted fake LLM.

Skipped when no PostgreSQL warehouse is configured. Proves the pieces fit end to end (engine, tool
layer, read-only role, services) and that a conversation, including hostile model behavior, leaves
the warehouse unchanged. No Gemini or other external request is made."""
import json

import pytest
from analyst_testlib import ScriptedLlm, call, say, use
from api_testlib import warehouse_settings
from sqlalchemy import text

from api.analyst import tools
from api.analyst.engine import Budget, run_chat
from api.db import create_engine
from pipeline import config
from pipeline.fingerprint import fingerprint

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module')
def wh():
    settings = warehouse_settings()
    engine = create_engine(settings)
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1 FROM gold.fct_activation_daily LIMIT 1'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built or the API role is not provisioned: {exc}')
    owner = config.create_engine(settings.postgres_db)
    before = fingerprint(owner, schemas=('gold', 'analytics'))
    yield settings, engine, owner, before
    owner.dispose()
    engine.dispose()


def user(text_):
    return {'role': 'user', 'content': text_}


def test_a_scripted_conversation_runs_real_tools_end_to_end(wh):
    settings, engine, _owner, _before = wh
    llm = ScriptedLlm(use(call('get_overview'), call('list_experiments'),
                          call('get_nps', granularity='month', min_responses=30)),
                      say('Answered from the three tool results.'))
    result = run_chat([user('Give me an overview, the experiments and NPS.')], llm=llm,
                      engine=engine, settings=settings, budget=Budget(max_requests=60, max_tokens=150_000))
    assert result.status == 'answered' and result.error is None
    assert [t['name'] for t in result.tool_trace] == ['get_overview', 'list_experiments', 'get_nps']
    assert all(t['result']['ok'] for t in result.tool_trace) and len(result.tool_results()) == 3
    for ok in result.tool_results():
        assert ok['meta']['data_version'] and ok['meta']['sources'] and ok['source']['endpoint'].startswith('/api/')
    shown = [json.loads(r['content']) for r in llm.requests[1].messages[-1]['results']]
    assert [s['tool'] for s in shown] == ['get_overview', 'list_experiments', 'get_nps']
    assert result.usage['requests'] == 2 and result.limits['tool_calls_used'] == 3


def test_the_tool_limit_holds_against_the_real_database(wh):
    settings, engine, _owner, _before = wh
    calls = [call('get_nps', min_responses=10 + i) for i in range(8)]
    llm = ScriptedLlm(use(*calls), say('Stopped at the limit.'))
    result = run_chat([user('many nps variants')], llm=llm, engine=engine, settings=settings)
    assert result.status == 'answered' and result.limits['limit_reached'] is True
    assert sum(1 for t in result.tool_trace if t['result']['ok']) == 6
    assert [t['result']['error']['code'] for t in result.tool_trace[6:]] == ['tool-limit', 'tool-limit']
    assert llm.requests[1].tools == ()


def test_hostile_model_behavior_leaves_the_warehouse_unchanged(wh):
    settings, engine, owner, before = wh
    attempts = [call('run_sql', query='DROP TABLE gold.fct_revenue_monthly'),
                call('get_support', call_type="x'; DELETE FROM gold.fct_support_daily;--"),
                call('get_feature_adoption', features=["x'; DROP SCHEMA gold CASCADE;--"]),
                call('get_experiment', experiment_id='no_such_experiment')]
    llm = ScriptedLlm(use(*attempts), say('I cannot do that.'))
    result = run_chat([user('drop everything')], llm=llm, engine=engine, settings=settings)
    assert result.status == 'answered' and not result.tool_results()
    assert [t['result']['error']['code'] for t in result.tool_trace] == [
        'unknown-tool', 'invalid-argument', 'invalid-parameter', 'experiment-not-found']
    assert fingerprint(owner, schemas=('gold', 'analytics')) == before


def test_every_approved_tool_is_offered_and_reachable(wh):
    settings, engine, _owner, _before = wh
    llm = ScriptedLlm(say('nothing needed'))
    run_chat([user('hello')], llm=llm, engine=engine, settings=settings)
    assert tuple(t['name'] for t in llm.requests[0].tools) == tools.TOOL_NAMES
