# Airflow + the ConnectHub pipeline.
#
# Airflow 2.8 pins SQLAlchemy < 2.0 while pandas 2.2 needs >= 2.0, so the
# pipeline does not share Airflow's environment: it gets its own virtualenv
# (/opt/pipeline-venv) installed from the project's lock file, and the DAG
# runs each step as `/opt/pipeline-venv/bin/python -m pipeline step <name>`.
# No Java/Spark here: Spark is not part of the pipeline (see docs/architecture.md).
FROM apache/airflow:2.8.1-python3.11

USER root
RUN mkdir -p /opt/pipeline-venv /opt/connecthub/data /opt/connecthub/dbt_runtime \
    && chown -R airflow:root /opt/pipeline-venv /opt/connecthub
USER airflow

# pytest in Airflow's own environment, pinned by Airflow's constraints, for the DAG test.
RUN pip install --no-cache-dir "pytest" \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.8.1/constraints-3.11.txt"

COPY --chown=airflow:root requirements/ /opt/connecthub/requirements/
# The base image sets PIP_USER=true, which a virtualenv rejects.
RUN python -m venv /opt/pipeline-venv \
    && PIP_USER=false /opt/pipeline-venv/bin/pip install --no-cache-dir --upgrade "pip<25" \
    && PIP_USER=false /opt/pipeline-venv/bin/pip install --no-cache-dir \
        -r /opt/connecthub/requirements/pipeline.txt \
        -c /opt/connecthub/requirements/constraints-py311.txt \
    && /opt/pipeline-venv/bin/pip check

COPY --chown=airflow:root pyproject.toml /opt/connecthub/
COPY --chown=airflow:root analytics/ /opt/connecthub/analytics/
COPY --chown=airflow:root experimentation/ /opt/connecthub/experimentation/
COPY --chown=airflow:root pipeline/ /opt/connecthub/pipeline/
COPY --chown=airflow:root quality/ /opt/connecthub/quality/
COPY --chown=airflow:root scripts/ /opt/connecthub/scripts/
COPY --chown=airflow:root dbt_project/ /opt/connecthub/dbt_project/
COPY --chown=airflow:root dags/ /opt/connecthub/dags/
COPY --chown=airflow:root tests/test_dag.py /opt/connecthub/tests/test_dag.py
COPY --chown=airflow:root docker/airflow/init.sh /opt/connecthub/docker/airflow/init.sh

ENV CONNECTHUB_HOME=/opt/connecthub \
    CONNECTHUB_DATA_DIR=/opt/connecthub/data \
    PIPELINE_PYTHON=/opt/pipeline-venv/bin/python \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/connecthub/dags \
    DBT_TARGET_PATH=/opt/connecthub/dbt_runtime/target \
    DBT_LOG_PATH=/opt/connecthub/dbt_runtime/logs \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    GX_ANALYTICS_ENABLED=False
WORKDIR /opt/connecthub
