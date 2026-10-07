"""AI analyst configuration (PHASE_6_PLAN.md step 1): off by default, the Gemini key comes only from
the process environment and never appears in a summary, a log line, an error or a tracked file.
No test here contacts Gemini; the key used is a made-up value."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from api_testlib import FAKE_PASSWORD, clean_env, make_settings  # noqa: F401
from pydantic import ValidationError

from api.settings import Settings, describe_validation_error
from pipeline import config
from pipeline.log import redact

pytestmark = pytest.mark.usefixtures('clean_env')

FAKE_GEMINI = 'fake-gemini-key-for-tests-0123456789'
ROOT = Path(config.PROJECT_ROOT)


def _env_settings(monkeypatch, **env):
    monkeypatch.setenv('API_DB_PASSWORD', FAKE_PASSWORD)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_the_analyst_is_off_and_unconfigured_by_default(monkeypatch):
    s = _env_settings(monkeypatch)
    assert s.analyst_enabled is False and s.gemini_api_key is None and s.analyst_model is None
    assert s.analyst_key_present is False and s.analyst_ready is False
    assert (s.analyst_max_tool_calls, s.analyst_log_content) == (6, False)


def test_the_key_is_read_from_the_process_environment(monkeypatch):
    s = _env_settings(monkeypatch, GEMINI_API_KEY=FAKE_GEMINI)
    assert s.gemini_api_key.get_secret_value() == FAKE_GEMINI
    assert s.analyst_key_present is True
    assert s.analyst_ready is False            # still off: ANALYST_ENABLED defaults to false


def test_a_dotenv_file_is_never_read(monkeypatch, tmp_path):
    (tmp_path / '.env').write_text(f'GEMINI_API_KEY={FAKE_GEMINI}\nANALYST_ENABLED=true\n')
    monkeypatch.chdir(tmp_path)
    s = _env_settings(monkeypatch)
    assert s.gemini_api_key is None and s.analyst_enabled is False


def test_ready_needs_all_of_enabled_key_and_model(monkeypatch):
    base = {'GEMINI_API_KEY': FAKE_GEMINI, 'ANALYST_ENABLED': 'true', 'ANALYST_MODEL': 'some-model-1'}
    assert _env_settings(monkeypatch, **base).analyst_ready is True
    for missing in base:
        monkeypatch.delenv(missing)
        assert Settings().analyst_ready is False, missing
        monkeypatch.setenv(missing, base[missing])


def test_enabled_without_a_key_does_not_stop_startup(monkeypatch):
    s = _env_settings(monkeypatch, ANALYST_ENABLED='true')
    assert s.analyst_enabled is True and s.analyst_ready is False


@pytest.mark.parametrize('blank', ['', '   '])
def test_a_blank_key_or_model_counts_as_unset(monkeypatch, blank):
    s = _env_settings(monkeypatch, GEMINI_API_KEY=blank, ANALYST_MODEL=blank)
    assert s.gemini_api_key is None and s.analyst_model is None


def test_the_key_is_masked_everywhere_it_could_be_printed(monkeypatch):
    s = _env_settings(monkeypatch, GEMINI_API_KEY=FAKE_GEMINI, ANALYST_ENABLED='true',
                      ANALYST_MODEL='some-model-1')
    summary = s.summary()
    assert summary['gemini_api_key'] == '***' and summary['analyst_ready'] is True
    for text in (repr(s), str(s), str(summary), json.dumps(summary), s.model_dump_json(),
                 repr(s.gemini_api_key), str(s.gemini_api_key)):
        assert FAKE_GEMINI not in text


def test_the_summary_shows_the_key_only_as_present_or_absent():
    assert make_settings().summary()['gemini_api_key'] is None
    assert make_settings(gemini_api_key=FAKE_GEMINI).summary()['gemini_api_key'] == '***'


def test_log_redaction_covers_the_key(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', FAKE_GEMINI)
    out = redact(f'upstream said: bad key {FAKE_GEMINI}')
    assert FAKE_GEMINI not in out and '***' in out


@pytest.mark.parametrize('field,value', [
    ('analyst_max_tool_calls', 7), ('analyst_max_tool_calls', 0), ('analyst_max_history_turns', 0),
    ('analyst_max_message_chars', 0), ('analyst_max_output_tokens', 10), ('analyst_max_tool_result_bytes', 100),
    ('analyst_upstream_timeout_s', 0), ('analyst_rate_limit_per_min', 0),
    ('analyst_daily_token_budget', 10), ('analyst_model', 'bad model; rm -rf')])
def test_invalid_analyst_limits_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_validation_messages_never_echo_the_key(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', FAKE_GEMINI)
    with pytest.raises(ValidationError) as info:
        make_settings(gemini_api_key=FAKE_GEMINI, analyst_max_tool_calls=99)
    message = describe_validation_error(info.value)
    assert message.startswith('ANALYST_MAX_TOOL_CALLS:') and FAKE_GEMINI not in message


def test_startup_failure_output_never_contains_the_key():
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('API_', 'ANALYST_', 'GEMINI_'))}
    env.update({'GEMINI_API_KEY': FAKE_GEMINI, 'API_CORS_ORIGINS': '*'})
    result = subprocess.run([sys.executable, '-m', 'api'], cwd=config.PROJECT_ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and 'API_CORS_ORIGINS' in result.stderr
    assert FAKE_GEMINI not in result.stderr + result.stdout


# --- the key must not be in any file --------------------------------------------------------

def _tracked_text_files():
    """Text files that could hold a secret: the repository's own files, not the virtualenv."""
    skip = {'.venv', '.git', 'node_modules', '__pycache__', '.pytest_cache', 'data', 'vendor'}
    suffixes = {'.py', '.md', '.txt', '.yml', '.yaml', '.html', '.mjs', '.json', '.toml', '.cfg',
                '.example', '.sql', '.ini', ''}
    for path in ROOT.rglob('*'):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & skip) \
                and path.suffix in suffixes and path.stat().st_size < 2_000_000:
            yield path


# Shapes of Google API credentials: "AIza" keys, and the "AQ." form of newer keys.
_GOOGLE_KEY_SHAPES = re.compile(r'AIza[0-9A-Za-z_-]{35}|\bAQ\.[A-Za-z0-9_-]{20,}')


def test_no_file_contains_something_shaped_like_a_google_api_key():
    hits = [str(p.relative_to(ROOT)) for p in _tracked_text_files()
            if _GOOGLE_KEY_SHAPES.search(p.read_text(encoding='utf-8', errors='ignore'))]
    assert hits == []


def test_env_example_names_the_variable_without_a_value():
    text = (ROOT / '.env.example').read_text(encoding='utf-8')
    assert 'GEMINI_API_KEY' in text
    assert not re.search(r'^\s*#?\s*GEMINI_API_KEY\s*=\s*\S', text, re.MULTILINE)
    assert re.search(r'^#\s+ANALYST_ENABLED=false', text, re.MULTILINE)


@pytest.mark.parametrize('name', ['docker/api/Dockerfile', '.dockerignore'])
def test_the_image_build_never_mentions_the_key(name):
    assert 'GEMINI' not in (ROOT / name).read_text(encoding='utf-8').upper()


def test_the_page_and_the_api_package_never_read_the_key_except_the_settings():
    page = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert 'GEMINI' not in page.upper()
    users = [p.name for p in (ROOT / 'api').rglob('*.py')
             if 'gemini_api_key' in p.read_text(encoding='utf-8')]
    assert users == ['settings.py']


def test_the_sdk_is_pinned_and_optional():
    pin = re.search(r'^google-genai==(\d+\.\d+\.\d+)$',
                    (ROOT / 'requirements' / 'analyst.txt').read_text(encoding='utf-8'), re.MULTILINE)
    assert pin
    lock = (ROOT / 'requirements' / 'constraints-py311.txt').read_text(encoding='utf-8')
    assert f'google-genai=={pin.group(1)}' in lock
    for name in ('api.txt', 'app.txt', 'dev.txt', 'pipeline.txt'):
        assert 'google-genai' not in (ROOT / 'requirements' / name).read_text(encoding='utf-8')
