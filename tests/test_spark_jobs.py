"""Spark jobs: executable on the generated data and consistent with dbt.

Needs pyspark + Java, so it runs in the isolated Spark image:

    docker compose --profile spark run --rm spark python -m pytest tests/test_spark_jobs.py -v
"""
import os
import shutil

import pandas as pd
import pytest

pyspark = pytest.importorskip('pyspark', reason='pyspark is not in the dev venv; run this '
                                                'test in the Spark container')
if not (os.environ.get('JAVA_HOME') or shutil.which('java')):
    pytest.skip('Java is not available; run this test in the Spark container',
                allow_module_level=True)

from pyspark.sql import SparkSession  # noqa: E402

from analytics.features import FEATURE_MAP  # noqa: E402
from spark_jobs.feature_extraction import extract_features  # noqa: E402
from spark_jobs.sessionize_events import sessionize  # noqa: E402

EVENTS = os.path.join(os.environ.get('CONNECTHUB_DATA_DIR', 'data'), 'events.parquet')


@pytest.fixture(scope='module')
def spark():
    session = (SparkSession.builder.master('local[2]').appName('connecthub-tests')
               .config('spark.sql.session.timeZone', 'UTC')
               .config('spark.sql.shuffle.partitions', '8')
               .config('spark.driver.memory', '1g').getOrCreate())
    yield session
    session.stop()


@pytest.fixture(scope='module')
def events(spark):
    if not os.path.exists(EVENTS):
        pytest.fail(f'{EVENTS} not found: run the pipeline generate step first')
    return spark.read.parquet(EVENTS)


def test_sessionize_splits_on_30_minute_gaps(spark):
    rows = [  # (event_id, user_id, timestamp)
        ('e1', 'u1', '2025-03-01 10:00:00'),
        ('e2', 'u1', '2025-03-01 10:30:00'),  # gap exactly 30 min: same session
        ('e3', 'u1', '2025-03-01 11:00:01'),  # gap 30 min 1 s: new session
        ('e4', 'u2', '2025-03-01 10:05:00'),
    ]
    df = spark.createDataFrame(pd.DataFrame(rows, columns=['event_id', 'user_id', 'ts'])) \
        .selectExpr('event_id', 'user_id', 'CAST(ts AS TIMESTAMP) AS timestamp_utc')
    out = sessionize(df).toPandas().set_index('event_id')
    assert out.loc['e1', 'spark_session_id'] == out.loc['e2', 'spark_session_id']
    assert out.loc['e3', 'spark_session_id'] != out.loc['e2', 'spark_session_id']
    assert out.loc['e2', 'session_duration_min'] == 30.0
    assert out['spark_session_id'].nunique() == 3


def test_feature_extraction_matches_pandas_reference(events):
    spark_usage = extract_features(events).select(
        'user_id', 'workspace_id', 'event_date', 'feature_name', 'usage_count').toPandas()
    raw = pd.read_parquet(EVENTS, columns=['user_id', 'workspace_id', 'event_date', 'event_name'])
    raw['feature_name'] = raw['event_name'].map(FEATURE_MAP)
    expected = raw.dropna(subset=['feature_name']).groupby(
        ['user_id', 'workspace_id', 'event_date', 'feature_name']).size() \
        .rename('usage_count').reset_index()
    key = ['user_id', 'workspace_id', 'event_date', 'feature_name']
    pd.testing.assert_frame_equal(
        spark_usage.sort_values(key).reset_index(drop=True),
        expected.sort_values(key).reset_index(drop=True), check_dtype=False)


def test_feature_extraction_matches_dbt_int_feature_usage(events):
    if not os.environ.get('POSTGRES_PASSWORD'):
        pytest.fail('POSTGRES_* not set: run via docker compose so the warehouse is reachable')
    from sqlalchemy import URL, create_engine
    engine = create_engine(URL.create(
        'postgresql+psycopg2', username=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'], host=os.environ['POSTGRES_HOST'],
        port=int(os.environ.get('POSTGRES_PORT', 5432)), database=os.environ['POSTGRES_DB']))
    dbt = pd.read_sql('SELECT user_id, workspace_id, event_date, feature_name, usage_count '
                      'FROM intermediate.int_feature_usage', engine)
    key = ['user_id', 'workspace_id', 'event_date', 'feature_name']
    spark_usage = extract_features(events).select(*key, 'usage_count').toPandas()
    for df in (dbt, spark_usage):
        df['event_date'] = pd.to_datetime(df['event_date'])
    pd.testing.assert_frame_equal(
        spark_usage.sort_values(key).reset_index(drop=True),
        dbt.sort_values(key).reset_index(drop=True), check_dtype=False)


def test_gap_sessionization_recovers_generated_sessions(events):
    """The generator's sessions are minutes-apart events, hours apart from each
    other; 30-minute-gap sessionization should recover nearly all of them."""
    pairs = sessionize(events).select('session_id', 'spark_session_id').distinct().toPandas()
    generated = pairs['session_id'].nunique()
    derived = pairs['spark_session_id'].nunique()
    one_to_one = (pairs.groupby('spark_session_id')['session_id'].nunique() == 1).mean()
    print(f'generated sessions {generated:,}, 30-min-gap sessions {derived:,}, '
          f'pure sessions {one_to_one:.4%}')
    assert one_to_one > 0.99            # a derived session almost never merges two
    assert 0.98 < generated / derived <= 1.0  # splits come only from gaps > 30 min
