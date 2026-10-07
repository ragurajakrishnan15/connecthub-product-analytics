"""The API contract must match the committed snapshot (docs/openapi.json).

A deliberate change (route, method, parameter, response schema, ...) is made by
regenerating the snapshot: `python -m api.openapi --write` (or `make openapi`),
reviewing the diff of docs/openapi.json and committing it with the change."""
import json

from api.openapi import SNAPSHOT, current_schema, diff, read_snapshot, render


def test_openapi_schema_matches_the_committed_snapshot():
    snapshot, schema = read_snapshot(), current_schema()
    assert snapshot == schema, ('API contract changed; if deliberate run '
                                '`python -m api.openapi --write`:\n' + diff(snapshot, schema))


def test_a_contract_change_is_detected():
    """The comparison is sensitive: an extra route or parameter breaks it."""
    from fastapi import Query

    from api.main import create_app
    from api.settings import Settings
    app = create_app(Settings(api_db_password='snapshot-only', api_docs_enabled=True))

    @app.get('/api/extra')
    def extra(flag: bool = Query(False)):
        return {}
    changed = app.openapi()
    assert changed != read_snapshot() and '/api/extra' in diff(read_snapshot(), changed)
    tampered = current_schema()
    tampered['paths']['/api/nps']['get']['parameters'].pop()
    assert tampered != read_snapshot()


def test_generation_is_deterministic():
    assert render(current_schema()) == render(current_schema())


def test_snapshot_is_stored_canonically():
    with open(SNAPSHOT, encoding='utf-8') as f:
        text = f.read().replace('\r\n', '\n')       # git may check out with CRLF on Windows
    assert text == render(json.loads(text))


def test_snapshot_covers_the_whole_contract():
    """Guard against a hollow snapshot: every route, method, parameter and schema is in it."""
    snapshot = read_snapshot()
    paths = snapshot['paths']
    assert len(paths) == 17
    assert {p for p, ops in paths.items() if set(ops) != {'get'}} == {'/api/analyst/chat'}
    assert set(paths['/api/analyst/chat']) == {'post'}      # Phase 6 adds this one route, nothing else
    params = {p['name'] for p in paths['/api/customer-health/workspaces']['get']['parameters']}
    assert params == {'tier', 'plan_tier', 'sort', 'order', 'limit', 'offset'}
    sort = next(p for p in paths['/api/customer-health/workspaces']['get']['parameters']
                if p['name'] == 'sort')
    assert sort['schema']['enum'] == ['health_score', 'mrr_usd', 'seat_count', 'active_users_30d']
    nps = paths['/api/nps']['get']['responses']
    assert {'200', '304', '400', '401', '422', '503', '504'} <= set(nps)
    assert 'ETag' in nps['200']['headers'] and nps['304']['headers']['ETag']
    assert 'application/problem+json' in nps['503']['content']
    schemas = snapshot['components']['schemas']
    assert {'Problem', 'Kpi', 'ResponseMeta', 'NpsData', 'WorkspacePage', 'Readiness'} <= set(schemas)
    assert set(schemas['NpsValue']['properties']) >= {'nps', 'margin_of_error_95', 'responses',
                                                     'suppressed_reason'}
