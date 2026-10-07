"""No test can reach Google: tests/conftest.py makes resolving a Google host fail the test.

Phase 6 allows a live Gemini request only in the separately approved evaluation, never in the suite."""
import socket

import pytest
from api_testlib import clean_env, make_settings  # noqa: F401
from fastapi.testclient import TestClient

from api.analyst.gemini import GeminiClient
from api.main import create_app

pytestmark = pytest.mark.usefixtures('clean_env')


@pytest.mark.parametrize('host', ['generativelanguage.googleapis.com', 'GENERATIVELANGUAGE.GOOGLEAPIS.COM.',
                                  'www.google.com', 'fonts.gstatic.com', b'generativelanguage.googleapis.com'])
def test_a_google_host_cannot_be_resolved_in_a_test(host):
    with pytest.raises(AssertionError, match='tried to reach'):
        socket.getaddrinfo(host, 443)


def test_a_real_client_built_from_settings_cannot_reach_google():
    settings = make_settings(analyst_enabled=True, gemini_api_key='fake-gemini-key-for-tests-0123456789',
                             analyst_model='fake-model')
    app = create_app(settings)                       # builds the real SDK client; constructing it sends nothing
    assert isinstance(app.state.analyst_llm, GeminiClient)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post('/api/analyst/chat', json={'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 502                     # the guard refused the lookup: nothing left the machine
    assert 'fake-gemini-key' not in r.text
