"""Structured logging: JSON lines with run context, and no secrets."""
import io
import json
import logging

import pytest

from pipeline import log as plog


@pytest.fixture
def logger(monkeypatch):
    monkeypatch.setenv('POSTGRES_PASSWORD', 's3cr3t-pw-value')
    monkeypatch.setenv('AIRFLOW__WEBSERVER__SECRET_KEY', 'another-secret-key')
    stream = io.StringIO()
    base = logging.getLogger('connecthub.pipeline')
    base.handlers.clear()
    yield plog.get_logger('run-1', 'ingest', stream=stream), stream
    base.handlers.clear()


def lines(stream):
    return [json.loads(x) for x in stream.getvalue().splitlines()]


def test_records_are_json_with_run_and_step(logger):
    log, stream = logger
    log.info('step.done', rows=42)
    (rec,) = lines(stream)
    assert rec['run_id'] == 'run-1' and rec['step'] == 'ingest'
    assert rec['event'] == 'step.done' and rec['rows'] == 42


def test_secrets_are_redacted(logger):
    log, stream = logger
    log.info('connect', url='postgresql://user:s3cr3t-pw-value@host/db',
             note='key another-secret-key')
    text = stream.getvalue()
    assert 's3cr3t-pw-value' not in text and 'another-secret-key' not in text
    assert 'postgresql://user:***@host/db' in text


def test_timed_logs_failures_and_reraises(logger):
    log, stream = logger
    with pytest.raises(ValueError):
        with log.timed('load'):
            raise ValueError('bad input')
    events = [r['event'] for r in lines(stream)]
    assert events == ['load.start', 'load.failed']
    assert 'bad input' in lines(stream)[-1]['error']
