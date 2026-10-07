"""Shared helpers for the API tests (imported by the test modules; no conftest so
this directory cannot shadow the `api` package)."""
import io
import json
import os

import pytest
from fastapi.testclient import TestClient

from api import logging as api_logging
from api.main import create_app
from api.settings import Settings

FAKE_PASSWORD = 'not-a-real-password-123'
# A port nothing listens on: every database call fails fast with "connection refused".
DEAD_DB = {'POSTGRES_HOST': '127.0.0.1', 'POSTGRES_PORT': 1, 'api_db_connect_timeout_s': 1}


@pytest.fixture
def clean_env(monkeypatch):
    """Unit tests must not depend on the developer's API_*, ANALYST_* or GEMINI_* environment
    (a real GEMINI_API_KEY in the shell must never reach a test)."""
    for key in list(os.environ):
        if key.upper().startswith(('API_', 'ANALYST_', 'GEMINI_')):
            monkeypatch.delenv(key, raising=False)


def make_settings(**overrides):
    # The analyst fields are pinned so a developer's real GEMINI_API_KEY never reaches a test.
    values = {'api_db_password': FAKE_PASSWORD, **DEAD_DB,
              'analyst_enabled': False, 'gemini_api_key': None, 'analyst_model': None}
    values.update(overrides)
    return Settings(**values)


def client_for(settings=None, raise_server_exceptions=False, **overrides):
    """A TestClient (use as a context manager to run the app lifespan)."""
    app = create_app(settings or make_settings(**overrides))
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def capture_logs():
    """Redirect the API's JSON log lines into a buffer; returns a reader."""
    buffer = io.StringIO()
    api_logging.configure('INFO', stream=buffer)

    def lines():
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    return lines


def assert_problem(response, status, slug):
    assert response.status_code == status, response.text
    assert response.headers['content-type'].startswith('application/problem+json')
    body = response.json()
    assert body['type'] == f'urn:connecthub:problem:{slug}'
    assert body['status'] == status
    assert body['request_id'] == response.headers['x-request-id']
    assert body['instance'] and body['title'] and body['detail']
    return body


def warehouse_settings():
    """Settings for the real local warehouse, or skip when it is not configured."""
    if not os.environ.get('POSTGRES_PASSWORD') or not os.environ.get('API_DB_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD / API_DB_PASSWORD not set; no PostgreSQL configured')
    return Settings(analyst_enabled=False, gemini_api_key=None, analyst_model=None)
