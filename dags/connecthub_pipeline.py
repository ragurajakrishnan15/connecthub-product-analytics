"""
ConnectHub analytics pipeline DAG.

generate -> assign -> ingest -> validate_bronze -> dbt -> validate_gold
         -> analytics -> validate_analytics

Every task runs one idempotent pipeline step in the project's own virtualenv
(`$PIPELINE_PYTHON -m pipeline step <name>`, see pipeline/steps.py), so the
DAG is safe to rerun: an unchanged dataset is not regenerated or reloaded,
assignments are insert-if-absent, and dbt rebuilds deterministically. All
tasks of one DAG run share the Airflow run_id as the pipeline run ID, which
appears in every log line and in ops.pipeline_runs.

Params (Trigger DAG w/ config):
    users           dataset size (default 10000)
    seed            generator seed (default 42)
    partition_date  YYYY-MM-DD: load only that day's events (incremental);
                    empty = full load
    full_refresh    force `dbt build --full-refresh`
"""
import os
from datetime import datetime, timedelta

from airflow import DAG
from airflow.models.param import Param
from airflow.operators.bash import BashOperator

STEPS = ['generate', 'assign', 'ingest', 'validate_bronze', 'dbt', 'validate_gold',
         'analytics', 'validate_analytics']
PROJECT = os.environ.get('CONNECTHUB_HOME', '/opt/connecthub')
PYTHON = os.environ.get('PIPELINE_PYTHON', '/opt/pipeline-venv/bin/python')

# __STEP__ is substituted per task; the rest is Jinja rendered by Airflow.
STEP_COMMAND = (
    PYTHON + ' -m pipeline step __STEP__ --run-id "{{ run_id }}" '
    '--users {{ params.users }} --seed {{ params.seed }} '
    '{% if params.partition_date %}--start-date {{ params.partition_date }} {% endif %}'
    '{% if params.full_refresh %}--full-refresh{% endif %}'
)

default_args = {
    'owner': 'data-team',
    'retries': 1,
    'retry_delay': timedelta(minutes=1),
    'depends_on_past': False,
    'email_on_failure': False,
}

with DAG(
    'connecthub_pipeline',
    description='Generate -> assign -> ingest -> validate -> dbt -> validate -> analytics -> validate',
    default_args=default_args,
    schedule='@daily',
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=['connecthub', 'analytics'],
    params={
        'users': Param(10_000, type='integer', minimum=10),
        'seed': Param(42, type='integer'),
        'partition_date': Param('', type='string',
                                description='YYYY-MM-DD for an incremental daily load'),
        'full_refresh': Param(False, type='boolean'),
    },
) as dag:
    tasks = [
        BashOperator(
            task_id=step,
            bash_command=STEP_COMMAND.replace('__STEP__', step),
            cwd=PROJECT,
            append_env=True,
            env={'PYTHONUNBUFFERED': '1'},
            execution_timeout=timedelta(hours=2),
        )
        for step in STEPS
    ]
    for upstream, downstream in zip(tasks, tasks[1:]):
        upstream >> downstream
