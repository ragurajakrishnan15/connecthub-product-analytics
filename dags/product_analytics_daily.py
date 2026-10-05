"""
ConnectHub Product Analytics Daily DAG
Orchestrates the full pipeline: ingest → quality → dbt → analytics
"""
from airflow import DAG
from airflow.operators.bash import BashOperator
from datetime import datetime, timedelta


def slack_alert(context):
    """Send Slack alert on task failure."""
    task_instance = context.get('task_instance')
    dag_id = context.get('dag').dag_id
    task_id = task_instance.task_id
    execution_date = context.get('execution_date')
    log_url = task_instance.log_url
    print(f"ALERT: {dag_id}.{task_id} failed at {execution_date}. Logs: {log_url}")


default_args = {
    'owner': 'data-team',
    'retries': 2,
    'retry_delay': timedelta(minutes=5),
    'sla': timedelta(hours=2),
    'on_failure_callback': slack_alert,
    'depends_on_past': False,
    'email_on_failure': False,
}

with DAG(
    'product_analytics_daily',
    default_args=default_args,
    description='Daily product analytics pipeline: ingest → quality → dbt → analytics',
    schedule_interval='0 6 * * *',
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=['product', 'analytics', 'daily'],
    max_active_runs=1,
) as dag:

    # Layer 1: Ingestion
    ingest_events = BashOperator(
        task_id='ingest_raw_events',
        bash_command='python scripts/ingest_events.py --date {{ ds }}',
    )

    # Layer 2: Data Quality
    quality_check = BashOperator(
        task_id='run_great_expectations',
        bash_command='great_expectations checkpoint run events_checkpoint',
    )

    # Layer 3: dbt Transformations
    run_dbt_staging = BashOperator(
        task_id='dbt_run_staging',
        bash_command='cd dbt_project && dbt run --select path:models/staging',
    )

    run_dbt_intermediate = BashOperator(
        task_id='dbt_run_intermediate',
        bash_command='cd dbt_project && dbt run --select path:models/intermediate',
    )

    run_dbt_gold = BashOperator(
        task_id='dbt_run_gold',
        bash_command='cd dbt_project && dbt run --select path:models/gold path:models/semantic',
    )

    run_dbt_tests = BashOperator(
        task_id='dbt_test',
        bash_command='cd dbt_project && dbt test',
    )

    # Layer 4: Analytics
    run_cohort_analysis = BashOperator(
        task_id='cohort_analysis',
        bash_command='python -m analytics.cohort_engine --date {{ ds }}',
    )

    run_experiment_eval = BashOperator(
        task_id='experiment_evaluation',
        bash_command='python -m experimentation.evaluate --date {{ ds }}',
    )

    # Layer 5: PySpark jobs
    run_sessionization = BashOperator(
        task_id='spark_sessionize',
        bash_command='spark-submit spark_jobs/sessionize_events.py {{ ds }}',
    )

    run_feature_extraction = BashOperator(
        task_id='spark_feature_extraction',
        bash_command='spark-submit spark_jobs/feature_extraction.py',
    )

    # Dependency chain
    ingest_events >> quality_check >> run_sessionization
    run_sessionization >> run_feature_extraction >> run_dbt_staging
    run_dbt_staging >> run_dbt_intermediate >> run_dbt_gold
    run_dbt_gold >> run_dbt_tests
    run_dbt_tests >> [run_cohort_analysis, run_experiment_eval]
