"""Integration: the Python parquet paths agree with the dbt warehouse.

Needs the local pipeline to have run (data/ generated, loaded into PostgreSQL,
dbt built). Skips when Postgres is unreachable, the gold models are missing,
or data/ does not match what is loaded (e.g. regenerated but not reloaded).
"""
import json
import os

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, text

import ingest_events as ingest
from analytics.cohort_engine import retention_table
from analytics.health_scoring import INPUT_COLUMNS, health_inputs_from_parquet
from experimentation.evaluate import evaluate_experiment, evaluate_from_parquet

DATA = 'data'
pytestmark = pytest.mark.integration


@pytest.fixture(scope='module')
def warehouse():
    if not os.environ.get('POSTGRES_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD not set; no PostgreSQL configured')
    if not os.path.exists(os.path.join(DATA, 'events.parquet')):
        pytest.skip('data/ has not been generated')
    engine = create_engine(ingest.database_url())
    try:
        with engine.connect() as conn:
            loaded = conn.execute(text('SELECT COUNT(*) FROM bronze.events_raw')).scalar()
            conn.execute(text('SELECT 1 FROM gold.fct_experiment_user_metrics LIMIT 1'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built: {exc}')
    import pyarrow.parquet as pq
    if pq.ParquetFile(os.path.join(DATA, 'events.parquet')).metadata.num_rows != loaded:
        pytest.skip('data/ differs from what is loaded; rerun ingest and dbt')
    yield engine
    engine.dispose()


def test_experiment_evaluation_matches(warehouse):
    w = evaluate_experiment(warehouse, 'exp_onboarding_v2')
    p = evaluate_from_parquet(os.path.join(DATA, 'experiment_assignments.parquet'),
                              os.path.join(DATA, 'events.parquet'), 'exp_onboarding_v2')
    # Counts, decisions and flags must match exactly. Continuous statistics may
    # differ in the last digits: the warehouse rounds per-user session minutes as
    # NUMERIC (half away from zero), Python rounds floats.
    def compare(a, b, path=''):
        if isinstance(a, dict):
            assert set(a) == set(b), path
            for k in a:
                compare(a[k], b[k], f'{path}.{k}')
        elif isinstance(a, (bool, str, int)) and not isinstance(a, float):
            assert a == b, path
        elif isinstance(a, list):
            assert np.allclose(a, b, rtol=1e-3, atol=1e-4), path
        else:
            assert a == pytest.approx(b, rel=1e-3, abs=1e-4), path
    compare(w, p)
    assert json.dumps(w['sample_sizes']) == json.dumps(p['sample_sizes'])


def test_health_inputs_match(warehouse):
    w = pd.read_sql('SELECT * FROM gold.metrics_product_health', warehouse) \
        .set_index('workspace_id').sort_index()
    p = health_inputs_from_parquet(DATA).set_index('workspace_id').sort_index()
    assert list(w.index) == list(p.index)
    for col in INPUT_COLUMNS[1:]:
        if col in ('workspace_name', 'plan_tier', 'snapshot_date', 'used_ai_feature_30d'):
            assert (w[col].astype(str) == p[col].astype(str)).all(), col
        else:
            # Rounded columns may differ by one unit in the last place: PostgreSQL
            # rounds NUMERIC half away from zero, Python rounds the float.
            a, b = w[col].astype(float), p[col].astype(float)
            assert (a.isna() == b.isna()).all(), col
            assert np.allclose(a.fillna(0), b.fillna(0), atol=0.0101), col


def test_retention_matches(warehouse):
    w = pd.read_sql('SELECT * FROM gold.fct_retention_cohorts ORDER BY 1, 2', warehouse)
    p = retention_table(pd.read_parquet(os.path.join(DATA, 'users.parquet')),
                        pd.read_parquet(os.path.join(DATA, 'events.parquet')))
    w['cohort_week'] = pd.to_datetime(w['cohort_week'])
    pd.testing.assert_frame_equal(
        w[['cohort_week', 'weeks_since_signup', 'active_users', 'cohort_size']]
        .reset_index(drop=True),
        p[['cohort_week', 'weeks_since_signup', 'active_users', 'cohort_size']],
        check_dtype=False)
    # retention_rate is rounded to 2 decimals; ties may round differently (see above).
    assert np.allclose(w['retention_rate'].astype(float), p['retention_rate'], atol=0.0101)
