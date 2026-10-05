"""DAG integrity. Needs Airflow, so it runs inside the Airflow image:

    docker compose run --rm airflow-scheduler python -m pytest tests/test_dag.py
"""
import os

import pytest

pytest.importorskip('airflow', reason='Airflow is not installed in the dev venv; '
                                      'run this test in the Airflow container')

from airflow.models import DagBag  # noqa: E402

DAG_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'dags')
EXPECTED = ['generate', 'assign', 'ingest', 'validate_bronze', 'dbt', 'validate_gold',
            'analytics', 'validate_analytics']


@pytest.fixture(scope='module')
def dagbag():
    return DagBag(dag_folder=DAG_FOLDER, include_examples=False)


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_tasks_form_the_expected_chain(dagbag):
    dag = dagbag.get_dag('connecthub_pipeline')
    assert dag is not None
    assert sorted(dag.task_ids) == sorted(EXPECTED)
    for upstream, downstream in zip(EXPECTED, EXPECTED[1:]):
        assert dag.get_task(downstream).upstream_task_ids == {upstream}


def test_commands_render_without_secrets(dagbag):
    dag = dagbag.get_dag('connecthub_pipeline')
    for task in dag.tasks:
        cmd = task.bash_command
        assert f'-m pipeline step {task.task_id}' in cmd
        assert 'PASSWORD' not in cmd.upper()
        assert os.environ.get('POSTGRES_PASSWORD', '\0') not in cmd
    assert dag.max_active_runs == 1 and not dag.catchup
