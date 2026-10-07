"""Analyst tools against the real local warehouse (PHASE_6_PLAN.md step 2).

Skipped when no PostgreSQL warehouse is configured or built, like the other integration tests.
What it proves that the unit tests cannot: each tool returns exactly what its REST route returns for
the same inputs (so no business logic was duplicated or altered), the connection is the API's
read-only role in a read-only transaction, real service errors map to stable codes, and running
every tool, including hostile arguments, leaves the warehouse byte-for-byte unchanged.
No Gemini or other external request is made."""
import json
from types import SimpleNamespace

import pytest
from api_testlib import client_for, warehouse_settings
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from api.analyst import tools
from api.db import create_engine
from pipeline import config
from pipeline.fingerprint import fingerprint

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module')
def wh():
    settings = warehouse_settings().model_copy(update={'analyst_max_tool_result_bytes': 500_000})
    engine = create_engine(settings)
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1 FROM gold.fct_activation_daily LIMIT 1'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built or the API role is not provisioned: {exc}')
    owner = config.create_engine(settings.postgres_db)
    before = fingerprint(owner, schemas=('gold', 'analytics'))
    with client_for(settings) as client:
        yield SimpleNamespace(settings=settings, engine=engine, client=client, owner=owner,
                              before=before)
    owner.dispose()
    engine.dispose()


def call(wh, name, arguments=None):
    return tools.run_tool(name, arguments, engine=wh.engine, settings=wh.settings)


def first_experiment_id(wh):
    return call(wh, 'list_experiments')['data']['experiments'][0]['experiment']['experiment_id']


def http_data(wh, path, params=None):
    response = wh.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# tool, arguments, REST path, REST query parameters for the same request
SAME_AS_REST = [
    ('get_meta', {}, '/api/meta', {}),
    ('get_overview', {}, '/api/overview', {}),
    ('get_engagement', {}, '/api/engagement', {}),
    ('get_engagement', {'start': '2025-03-01', 'end': '2025-03-31', 'granularity': 'day'},
     '/api/engagement', {'start': '2025-03-01', 'end': '2025-03-31', 'granularity': 'day'}),
    ('get_activation', {'plan_tier': 'Free', 'granularity': 'month'}, '/api/activation',
     {'plan_tier': 'Free', 'granularity': 'month'}),
    ('get_activation', {'include_incomplete': True}, '/api/activation', {'include_incomplete': 'true'}),
    ('get_retention', {}, '/api/retention', {}),
    ('get_retention', {'cohort_start': '2025-06-01', 'cohort_end': '2025-08-31'}, '/api/retention',
     {'cohort_start': '2025-06-01', 'cohort_end': '2025-08-31'}),
    ('get_cohorts', {'cohort_start': '2025-06-01', 'cohort_end': '2025-08-31', 'weeks': 8},
     '/api/cohorts', {'cohort_start': '2025-06-01', 'cohort_end': '2025-08-31', 'weeks': 8}),
    ('get_revenue', {}, '/api/revenue', {}),
    ('get_revenue', {'group_by': 'none', 'start_month': '2025-01', 'end_month': '2025-06'},
     '/api/revenue', {'group_by': 'none', 'start_month': '2025-01', 'end_month': '2025-06'}),
    ('get_revenue', {'plan_tier': 'Enterprise', 'group_by': 'none'}, '/api/revenue',
     {'plan_tier': 'Enterprise', 'group_by': 'none'}),
    ('get_feature_adoption', {'max_day': 30}, '/api/feature-adoption', {'max_day': 30}),
    ('list_experiments', {}, '/api/experiments', {}),
    ('get_nps', {}, '/api/nps', {}),
    ('get_nps', {'granularity': 'week', 'plan_tier': 'Professional', 'min_responses': 10},
     '/api/nps', {'granularity': 'week', 'plan_tier': 'Professional', 'min_responses': 10}),
    ('get_support', {}, '/api/support', {}),
    ('get_support', {'granularity': 'day', 'plan_tier': 'Free'}, '/api/support',
     {'granularity': 'day', 'plan_tier': 'Free'}),
    ('get_customer_health', {}, '/api/customer-health', {}),
    ('get_customer_health', {'plan_tier': 'Enterprise'}, '/api/customer-health',
     {'plan_tier': 'Enterprise'}),
    ('list_workspaces', {'limit': 25}, '/api/customer-health/workspaces', {'limit': 25}),
    ('list_workspaces', {'tier': 'Critical', 'sort': 'mrr_usd', 'order': 'desc', 'limit': 5,
                         'offset': 2},
     '/api/customer-health/workspaces',
     {'tier': 'Critical', 'sort': 'mrr_usd', 'order': 'desc', 'limit': 5, 'offset': 2}),
]


@pytest.mark.parametrize('name,arguments,path,params', SAME_AS_REST,
                         ids=[f'{c[0]}-{i}' for i, c in enumerate(SAME_AS_REST)])
def test_a_tool_returns_what_its_rest_route_returns(wh, name, arguments, path, params):
    result = call(wh, name, arguments)
    assert result['ok'] is True, result
    rest = http_data(wh, path, params)
    expected_data, shortened = tools.clean_for_model(rest['data'])
    assert result['data'] == expected_data
    assert result['limits']['truncated_lists'] == []
    assert result['meta']['data_version'] == rest['meta']['data_version']
    assert result['meta']['sources'] == rest['meta']['sources']
    assert result['meta']['as_of'] == rest['meta']['as_of']
    assert result['meta']['caveats'] == rest['meta']['caveats']
    assert result['meta']['effective_range'] == rest['meta']['effective_range']
    assert result['source']['endpoint'] == tools.TOOLS[name].endpoint
    assert result['limits']['truncated_strings'] == shortened


def test_feature_adoption_and_experiments_by_name_match_rest(wh):
    names = [f['name'] for f in call(wh, 'get_meta')['data']['features']]
    assert len(names) >= 2
    pair = names[:2]
    result = call(wh, 'get_feature_adoption', {'features': pair, 'max_day': 14})
    rest = http_data(wh, '/api/feature-adoption', {'features': pair, 'max_day': 14})
    assert result['ok'] and result['data'] == tools.clean_for_model(rest['data'])[0]

    experiments = call(wh, 'list_experiments')['data']['experiments']
    assert experiments
    for experiment in experiments:
        experiment_id = experiment['experiment']['experiment_id']
        detail = call(wh, 'get_experiment', {'experiment_id': experiment_id})
        rest = http_data(wh, f'/api/experiments/{experiment_id}')
        assert detail['ok'] and detail['data'] == tools.clean_for_model(rest['data'])[0]
        assert detail['source']['arguments'] == {'experiment_id': experiment_id}


def test_real_results_are_not_altered_by_cleaning_or_the_default_size_limit(wh):
    """With the shipped defaults no real result is truncated or has a string shortened (the caps
    are a safety net for hostile data, not something a normal answer should hit)."""
    default = wh.settings.model_copy(update={'analyst_max_tool_result_bytes': 48_000})
    sizes = {}
    for name in tools.TOOL_NAMES:
        arguments = {'experiment_id': first_experiment_id(wh)} if name == 'get_experiment' else {}
        result = tools.run_tool(name, arguments, engine=wh.engine, settings=default)
        assert result['ok'] is True, (name, result)
        sizes[name] = len(json.dumps(result).encode())
        assert result['limits']['truncated_strings'] == 0, name
        assert result['limits']['truncated_lists'] == [], (name, sizes[name])
    assert max(sizes.values()) <= 48_000, sizes


def test_a_large_request_is_cut_to_the_limit_and_still_valid(wh):
    small = wh.settings.model_copy(update={'analyst_max_tool_result_bytes': 3000})
    result = tools.run_tool('list_workspaces', {'limit': 100}, engine=wh.engine, settings=small)
    assert result['ok'] is True
    assert len(json.dumps(result, separators=(',', ':')).encode()) <= 3000
    [record] = result['limits']['truncated_lists']
    assert record['path'] == 'items' and record['kept'] < record['of'] == 100
    assert len(result['data']['items']) == record['kept']
    assert result['data']['total'] > record['kept']


def test_an_empty_page_is_a_valid_result(wh):
    result = call(wh, 'list_workspaces', {'offset': 10000, 'limit': 5})
    assert result['ok'] is True and result['data']['items'] == []
    assert result['data']['next_offset'] is None


def test_the_connection_is_the_api_role_in_a_read_only_transaction(wh):
    with tools.read_only_connection(wh.engine) as conn:
        assert conn.execute(text('SHOW transaction_read_only')).scalar() == 'on'
        assert conn.execute(text('SELECT current_user')).scalar() == wh.settings.api_db_user
    for statement in ('CREATE TEMP TABLE analyst_probe (a int)',
                      'CREATE TABLE gold.analyst_probe (a int)',
                      'DELETE FROM gold.fct_activation_daily'):
        with tools.read_only_connection(wh.engine) as conn:
            with pytest.raises(sa_exc.DBAPIError) as info:
                conn.execute(text(statement))
        assert 'read-only' in str(info.value) or 'permission denied' in str(info.value), statement


def test_real_service_errors_map_to_stable_codes(wh):
    cases = [
        ('get_experiment', {'experiment_id': 'no_such_experiment'}, 'experiment-not-found'),
        ('get_retention', {'as_of': '1999-01-01'}, 'invalid-range'),
        ('get_engagement', {'start': '2025-06-30', 'end': '2025-06-01'}, 'invalid-range'),
        ('get_engagement', {'start': '1990-01-01', 'end': '1990-02-01'}, 'invalid-range'),
        ('get_engagement', {'start': '2025-01-01', 'end': '2025-12-31', 'granularity': 'day'},
         'invalid-parameter'),
        ('get_revenue', {'plan_tier': 'Free', 'group_by': 'plan_tier'}, 'invalid-parameter'),
        ('get_support', {'call_type': 'no_such_call_type'}, 'invalid-parameter'),
        ('get_feature_adoption', {'features': ['no_such_feature']}, 'invalid-parameter'),
        ('get_cohorts', {'cohort_start': '2025-01-01', 'cohort_end': '2025-12-31'}, None),
    ]
    for name, arguments, code in cases:
        result = call(wh, name, arguments)
        if code is None:                       # allowed to succeed or to ask for fewer cohorts
            assert result['ok'] or result['error']['code'] == 'invalid-range'
            continue
        assert result['ok'] is False and result['error']['code'] == code, (name, result)
        assert result['error']['retryable'] is False


def test_an_unreachable_database_is_a_retryable_error_result(wh):
    dead = wh.settings.model_copy(update={'postgres_port': 1, 'api_db_connect_timeout_s': 1})
    result = tools.run_tool('get_overview', {}, engine=create_engine(dead), settings=dead)
    assert result['ok'] is False and result['error']['code'] == 'database-unavailable'
    assert result['error']['retryable'] is True
    assert dead.api_db_password.get_secret_value() not in json.dumps(result)


def test_hostile_arguments_and_values_never_change_the_warehouse(wh):
    hostile = [
        ('get_feature_adoption', {'features': ["x'; DROP TABLE gold.fct_revenue_monthly;--"]}),
        ('get_support', {'call_type': "a'; DELETE FROM gold.fct_support_daily;--"}),
        ('get_activation', {'plan_tier': "Free'; TRUNCATE gold.fct_activation_daily;--"}),
        ('get_experiment', {'experiment_id': "x'; DROP SCHEMA gold CASCADE;--"}),
        ('list_workspaces', {'sort': 'workspace_name; DROP TABLE x', 'limit': 5}),
        ('run_sql', {'query': 'DROP TABLE gold.fct_revenue_monthly'}),
    ]
    for name, arguments in hostile:
        result = call(wh, name, arguments)
        assert result['ok'] is False, (name, result)
    # the only hostile string that reaches a service (a feature name) is reported as plain text
    unknown = call(wh, 'get_feature_adoption', {'features': ["x'; DROP TABLE y;--"]})
    assert unknown['error']['code'] == 'invalid-parameter'
    # and then everything, once more, to be sure reads leave the warehouse alone
    for name in tools.TOOL_NAMES:
        arguments = {'experiment_id': first_experiment_id(wh)} if name == 'get_experiment' else {}
        assert call(wh, name, arguments)['ok'] is True, name
    assert fingerprint(wh.owner, schemas=('gold', 'analytics')) == wh.before
