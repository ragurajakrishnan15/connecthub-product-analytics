"""Tests for scripts/ingest_events.py PostgreSQL loading.

The integration tests need a reachable PostgreSQL configured through the
POSTGRES_* environment variables (see .env.example); they are skipped otherwise.
They write only to a throwaway schema, never to bronze/experiments.
"""
import os
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

import ingest_events as ingest


def test_prepare_frame_orders_columns_and_coerces_dates():
    cols = ingest.TABLES['bronze.users_raw']['columns']
    df = pd.DataFrame({
        'is_admin': [True],
        'user_id': ['u1'],
        'workspace_id': ['w1'],
        'signup_date': pd.to_datetime(['2025-03-04']),
        'first_active_date': pd.to_datetime(['2025-03-05']),
        'plan_tier': ['Free'],
        'country_code': ['US'],
        'extra': [1],
    })
    out = ingest.prepare_frame(df, cols)
    assert list(out.columns) == list(cols)
    assert str(out.loc[0, 'signup_date']) == '2025-03-04'


def test_prepare_frame_rejects_missing_columns():
    cols = ingest.TABLES['bronze.users_raw']['columns']
    with pytest.raises(ValueError, match='Missing columns'):
        ingest.prepare_frame(pd.DataFrame({'user_id': ['u1']}), cols)


@pytest.fixture
def pg_engine():
    if not os.environ.get('POSTGRES_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD not set; no PostgreSQL configured')
    engine = create_engine(ingest.database_url())
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'PostgreSQL not reachable: {exc}')
    schema = f'test_ingest_{uuid.uuid4().hex[:8]}'
    yield engine, schema
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
    engine.dispose()


@pytest.mark.integration
def test_copy_into_postgres_with_correct_types(pg_engine):
    engine, schema = pg_engine
    users = f'{schema}.users_raw'
    evals = f'{schema}.agent_evaluations'
    tables = {
        users: ingest.TABLES['bronze.users_raw'],
        evals: ingest.TABLES['bronze.agent_evaluations'],
    }
    ingest.create_tables(engine, tables)

    users_df = pd.DataFrame({
        'user_id': ['u1', 'u2'],
        'workspace_id': ['w1', None],
        'signup_date': pd.to_datetime(['2025-01-01', '2025-02-15']),
        'first_active_date': pd.to_datetime(['2025-01-02', None]),
        'plan_tier': ['Free', 'Enterprise'],
        'country_code': ['US', 'FR'],
        'is_admin': [True, False],
    })
    evals_df = pd.DataFrame({
        'eval_id': ['e1'],
        'call_date': pd.to_datetime(['2025-06-01']),
        'call_type': ['inbound'],
        'resolved_by_ai': [True],
        'handle_time_seconds': [123],
        'csat_score': [4.5],
        'escalated_to_human': [False],
    })
    assert ingest.copy_frame(engine, users_df, users, tables[users]['columns']) == 2
    assert ingest.copy_frame(engine, evals_df, evals, tables[evals]['columns']) == 1
    # Loading again replaces the contents instead of appending.
    assert ingest.copy_frame(engine, users_df, users, tables[users]['columns']) == 2

    with engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = 'users_raw'"), {'s': schema}).all())
        rows = conn.execute(text(
            f'SELECT user_id, workspace_id, signup_date, first_active_date, is_admin '
            f'FROM {users} ORDER BY user_id')).all()
        eval_row = conn.execute(text(
            f'SELECT handle_time_seconds, csat_score, resolved_by_ai FROM {evals}')).one()

    assert types['signup_date'] == 'date'
    assert types['first_active_date'] == 'date'
    assert types['is_admin'] == 'boolean'
    assert len(rows) == 2
    assert str(rows[0].signup_date) == '2025-01-01'
    assert rows[0].is_admin is True
    assert rows[1].workspace_id is None
    assert rows[1].first_active_date is None
    assert eval_row.handle_time_seconds == 123
    assert float(eval_row.csat_score) == 4.5
    assert eval_row.resolved_by_ai is True
