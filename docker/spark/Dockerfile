# Isolated Spark environment for spark_jobs/ and their integration test.
# Spark is not part of the production pipeline (see docs/architecture.md);
# this image exists so the jobs are executable and verified, not decorative.
# Pinned to bookworm: newer Debian (trixie) no longer packages OpenJDK 17.
FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless procps \
    && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

WORKDIR /opt/connecthub
COPY requirements/ requirements/
RUN pip install --no-cache-dir -r requirements/spark.txt \
        pandas pyarrow numpy SQLAlchemy psycopg2-binary pytest \
        -c requirements/constraints-py311.txt

COPY pyproject.toml ./
COPY spark_jobs/ spark_jobs/
COPY analytics/__init__.py analytics/features.py analytics/
COPY tests/test_spark_jobs.py tests/
ENV PYTHONPATH=/opt/connecthub
