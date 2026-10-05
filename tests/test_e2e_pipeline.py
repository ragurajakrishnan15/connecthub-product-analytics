"""End-to-end: the whole pipeline in a throwaway database (<POSTGRES_DB>_e2e).

1. Run the pipeline twice: the warehouses must be identical (idempotency), the
   rerun must not regenerate, reload or duplicate anything.
2. Incremental dbt on a new day == full refresh of the same data.
3. Corrupted bronze data must fail validation and therefore the pipeline.

Takes ~3-4 minutes. Needs PostgreSQL (POSTGRES_* in the environment).
"""
import json
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from pipeline import config
from pipeline.fingerprint import diff, fingerprint

pytestmark = pytest.mark.integration
USERS = 2000
PK = {  # table -> key columns
    'bronze.users_raw': 'user_id', 'bronze.workspaces_raw': 'workspace_id',
    'bronze.events_raw': 'event_id', 'bronze.subscriptions': 'workspace_id, month_start',
    'bronze.nps_responses': 'response_id', 'bronze.agent_evaluations': 'eval_id',
    'experiments.experiment_assignments': 'experiment_id, user_id',
    'staging.stg_events': 'event_id', 'intermediate.int_sessions': 'session_id',
}


@pytest.fixture(scope='module')
def e2e(tmp_path_factory):
    if not os.environ.get('POSTGRES_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD not set; no PostgreSQL configured')
    admin = config.create_engine().execution_options(isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as conn:
            conn.execute(text('SELECT 1'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'PostgreSQL not reachable: {exc}')
    db = f"{os.environ.get('POSTGRES_DB', 'connecthub_analytics')}_e2e"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db}"'))
    yield db, tmp_path_factory.mktemp('e2e_data')
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
    admin.dispose()


def pipeline(db, data_dir, *args, run_id=None):
    cmd = [sys.executable, '-m', 'pipeline', *args, '--database', db,
           '--users', str(USERS), '--data-dir', str(data_dir)]
    if run_id:
        cmd += ['--run-id', run_id]
    env = dict(os.environ, POSTGRES_HOST=os.environ.get('POSTGRES_HOST', '127.0.0.1'))
    return subprocess.run(cmd, cwd=config.PROJECT_ROOT, env=env, capture_output=True,
                          text=True, timeout=1200)


def step_metrics(engine, run_id):
    with engine.connect() as conn:
        rows = conn.execute(text('SELECT step, status, metrics FROM ops.pipeline_runs '
                                 'WHERE run_id = :r'), {'r': run_id}).all()
    return {step: (status, metrics) for step, status, metrics in rows}


def test_pipeline_is_idempotent_incremental_and_gated(e2e):
    db, data_dir = e2e
    engine = config.create_engine(db)
    run1, run2 = f'e2e-{uuid.uuid4().hex[:6]}-1', f'e2e-{uuid.uuid4().hex[:6]}-2'

    # --- run 1: everything from scratch
    first = pipeline(db, data_dir, 'run', run_id=run1)
    assert first.returncode == 0, first.stderr[-2000:]
    fp1 = fingerprint(engine)
    steps1 = step_metrics(engine, run1)
    assert all(status == 'success' for status, _ in steps1.values()) and len(steps1) == 8
    assert steps1['generate'][1]['skipped'] is False
    assert steps1['dbt'][1]['full_refresh'] is True

    # --- run 2: identical warehouse, nothing regenerated, reloaded or duplicated
    second = pipeline(db, data_dir, 'run', run_id=run2)
    assert second.returncode == 0, second.stderr[-2000:]
    fp2 = fingerprint(engine)
    assert diff(fp1, fp2) == [], 'rerun changed the warehouse'
    steps2 = step_metrics(engine, run2)
    assert steps2['generate'][1]['skipped'] is True
    assert steps2['assign'][1]['inserted'] == 0
    assert steps2['ingest'][1]['events_skipped_already_loaded'] is True
    assert steps2['dbt'][1]['full_refresh'] is False
    with engine.connect() as conn:
        for table, key in PK.items():
            n, distinct = conn.execute(text(
                f'SELECT COUNT(*), COUNT(DISTINCT ({key})) FROM {table}')).one()
            assert n == distinct > 0, f'duplicates in {table}'

    # --- incremental == full refresh
    verify = pipeline(db, data_dir, 'verify-incremental')
    assert verify.returncode == 0, verify.stderr[-2000:]
    result = json.loads(verify.stdout.strip().splitlines()[-1])
    assert result['identical'] and result['differing'] == []

    # --- corrupted bronze must fail validation, and the pipeline step with it
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO bronze.events_raw SELECT * FROM bronze.events_raw LIMIT 1"))  # dup PK
        conn.execute(text(
            "INSERT INTO bronze.events_raw (event_id, user_id, workspace_id, event_name, "
            "timestamp_utc, event_date) VALUES ('orphan-evt', 'no-such-user', NULL, "
            "'not.an.event', '2025-05-05 10:00', '2025-05-05')"))
        conn.execute(text("UPDATE bronze.users_raw SET plan_tier = 'Platinum' "
                          "WHERE user_id = (SELECT MIN(user_id) FROM bronze.users_raw)"))
        conn.execute(text("UPDATE bronze.subscriptions SET mrr_usd = -1 "
                          "WHERE ctid = (SELECT MIN(ctid) FROM bronze.subscriptions)"))
    from quality.validate import run_stage
    summary = run_stage('bronze', config.database_url(db))
    failed = {(f['asset'], f['expectation']) for f in summary['critical_failures']}
    assert not summary['success']
    assert ('duplicate_keys_bronze_events_raw', 'expect_table_row_count_to_equal') in failed
    assert ('orphan_events_user', 'expect_table_row_count_to_equal') in failed
    assert ('bronze.events_raw', 'expect_column_values_to_be_in_set') in failed
    assert ('bronze.users_raw', 'expect_column_values_to_be_in_set') in failed
    assert ('bronze.subscriptions', 'expect_column_values_to_be_between') in failed
    gated = pipeline(db, data_dir, 'step', 'validate_bronze')
    assert gated.returncode == 1
    assert 'bronze data-quality checks failed' in gated.stderr
    engine.dispose()
