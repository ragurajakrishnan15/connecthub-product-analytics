"""API configuration: validation, defaults, secret handling, startup failure."""
import os
import subprocess
import sys

import pytest
from api_testlib import FAKE_PASSWORD, clean_env, make_settings  # noqa: F401
from pydantic import ValidationError

from api.db import engine_url, session_options
from api.settings import Settings, describe_validation_error
from pipeline import config

pytestmark = pytest.mark.usefixtures('clean_env')


def test_password_is_required_and_the_error_names_the_variable():
    with pytest.raises(ValidationError) as info:
        Settings()
    assert describe_validation_error(info.value) == 'API_DB_PASSWORD: is required but not set'


def test_defaults_are_local_and_safe():
    s = make_settings()
    assert s.api_host == '127.0.0.1' and s.api_env == 'development'
    assert s.api_auth_mode == 'none' and s.docs_enabled is True
    assert s.api_cors_origins == ['http://127.0.0.1:8000', 'http://localhost:8000']
    assert s.api_db_user == 'connecthub_api'


def test_comma_separated_lists_from_the_environment(monkeypatch):
    monkeypatch.setenv('API_DB_PASSWORD', FAKE_PASSWORD)
    monkeypatch.setenv('API_CORS_ORIGINS', 'https://dash.example.com, http://localhost:5173')
    monkeypatch.setenv('API_AUTH_MODE', 'api_key')
    monkeypatch.setenv('API_KEYS', 'k' * 16 + ',' + 'q' * 20)
    s = Settings()
    assert s.api_cors_origins == ['https://dash.example.com', 'http://localhost:5173']
    assert [k.get_secret_value() for k in s.api_keys] == ['k' * 16, 'q' * 20]


@pytest.mark.parametrize('origins', ['*', 'null', 'http://ok.example.com,*', 'ftp://x.example',
                                     'http://host/path', 'example.com'])
def test_wildcard_null_and_malformed_cors_origins_are_rejected(origins):
    with pytest.raises(ValidationError) as info:
        make_settings(api_cors_origins=origins)
    assert describe_validation_error(info.value).startswith('API_CORS_ORIGINS:')


def test_production_requires_api_keys_and_disables_docs_by_default():
    with pytest.raises(ValidationError, match='production requires API_AUTH_MODE=api_key'):
        make_settings(api_env='production')
    s = make_settings(api_env='production', api_auth_mode='api_key', api_keys='x' * 32)
    assert s.docs_enabled is False
    assert make_settings(api_env='production', api_auth_mode='api_key', api_keys='x' * 32,
                         api_docs_enabled=True).docs_enabled is True


def test_api_key_mode_needs_long_enough_keys():
    with pytest.raises(ValidationError, match='needs at least one key'):
        make_settings(api_auth_mode='api_key')
    with pytest.raises(ValidationError, match='at least 16 characters'):
        make_settings(api_auth_mode='api_key', api_keys='short')


@pytest.mark.parametrize('field,value', [
    ('api_db_user', 'Robert"); DROP'), ('api_db_password', 'short'), ('POSTGRES_PORT', 70000),
    ('api_db_pool_size', 0), ('api_statement_timeout_ms', 10), ('api_env', 'staging'),
    ('api_log_level', 'TRACE')])
def test_invalid_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_validation_messages_never_echo_secret_values():
    secret = 'super-secret-key'          # 16 chars, but mode/env combination is invalid
    with pytest.raises(ValidationError) as info:
        make_settings(api_db_password=secret, api_env='production', api_keys=secret)
    assert secret not in describe_validation_error(info.value)


def test_summary_masks_secrets():
    s = make_settings(api_auth_mode='api_key', api_keys='y' * 24)
    summary = s.summary()
    assert summary['api_db_password'] == '***' and summary['api_keys'] == ['***']
    assert FAKE_PASSWORD not in str(summary) and 'y' * 24 not in str(summary)


def test_engine_uses_the_api_role_and_a_read_only_session(monkeypatch):
    monkeypatch.setenv('POSTGRES_USER', 'owner_role')
    s = make_settings(POSTGRES_HOST='db.internal', POSTGRES_PORT=6543, POSTGRES_DB='wh')
    url = engine_url(s)
    assert (url.username, url.host, url.port, url.database) == \
        ('connecthub_api', 'db.internal', 6543, 'wh')
    assert url.password == FAKE_PASSWORD
    options = session_options(s)
    for expected in ('default_transaction_read_only=on', 'statement_timeout=5000',
                     'lock_timeout=2000', 'idle_in_transaction_session_timeout=10000'):
        assert expected in options


def test_startup_fails_cleanly_without_configuration():
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('API_', 'ANALYST_', 'GEMINI_'))}
    env['POSTGRES_PASSWORD'] = 'owner-secret-value-xyz'
    env['API_CORS_ORIGINS'] = '*'
    result = subprocess.run([sys.executable, '-m', 'api'], cwd=config.PROJECT_ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert 'API_DB_PASSWORD: is required but not set' in result.stderr
    assert "API_CORS_ORIGINS: Value error, CORS origin '*' is not allowed" in result.stderr
    assert 'owner-secret-value-xyz' not in result.stderr + result.stdout
