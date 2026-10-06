"""
The API contract snapshot (docs/openapi.json).

    python -m api.openapi --check      exit 1 (with a diff) if the app's schema changed
    python -m api.openapi --write      regenerate the snapshot after a deliberate change

The schema is generated from the app with fixed settings (no database, no
environment influence) and serialized deterministically (sorted keys, 2-space
indent, LF). tests/api/test_api_openapi_snapshot.py runs the same comparison.
"""
import argparse
import difflib
import json
import os
import sys

from pipeline.config import PROJECT_ROOT

SNAPSHOT = os.path.join(PROJECT_ROOT, 'docs', 'openapi.json')


def current_schema():
    from api.main import create_app
    from api.settings import Settings
    settings = Settings(api_db_password='snapshot-only', api_env='development',
                        api_auth_mode='none', api_docs_enabled=True)
    return create_app(settings).openapi()


def render(schema):
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def read_snapshot(path=SNAPSHOT):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def diff(expected, actual, limit=80):
    lines = list(difflib.unified_diff(render(expected).splitlines(), render(actual).splitlines(),
                                      'docs/openapi.json (snapshot)', 'current app', lineterm=''))
    return '\n'.join(lines[:limit]) + ('\n...' if len(lines) > limit else '')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m api.openapi', description=__doc__.split('\n')[1])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--write', action='store_true')
    args = parser.parse_args(argv)
    schema = current_schema()
    if args.write:
        with open(SNAPSHOT, 'w', encoding='utf-8', newline='\n') as f:
            f.write(render(schema))
        print(f'wrote {os.path.relpath(SNAPSHOT, PROJECT_ROOT)} ({len(schema["paths"])} paths)')
        return 0
    snapshot = read_snapshot()
    if snapshot == schema:
        print(f'OpenAPI snapshot matches ({len(schema["paths"])} paths)')
        return 0
    print(diff(snapshot, schema))
    print('error: the API contract changed. If deliberate, run: python -m api.openapi --write '
          '(make openapi) and commit docs/openapi.json', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
