.PHONY: setup test test-fast lint pipeline pipeline-incremental verify-incremental validate \
        bench up down airflow-test spark-test clean

# Requires Python 3.11 (see .python-version). Configuration comes from the
# environment: copy .env.example to .env and export its variables first.
USERS ?= 10000
SEED ?= 42

# ==================== SETUP ====================
setup:
	pip install -r requirements.txt -c requirements/constraints-py311.txt

# ==================== PIPELINE ====================
pipeline:                 ## all steps: generate -> assign -> ingest -> validate -> dbt -> validate -> analytics -> validate
	python -m pipeline run --users $(USERS) --seed $(SEED)

pipeline-incremental:     ## load one day (DAY=YYYY-MM-DD) and build incrementally
	python -m pipeline run --users $(USERS) --seed $(SEED) --start-date $(DAY) \
		--steps ingest,validate_bronze,dbt,validate_gold,analytics,validate_analytics

verify-incremental:       ## prove incremental dbt == full refresh on the same data
	python -m pipeline verify-incremental --users $(USERS) --seed $(SEED)

validate:
	python -m quality.validate --stage all

bench:
	python scripts/benchmark.py --users $(USERS)

# ==================== DOCKER ====================
up:                       ## postgres + Airflow (UI on 127.0.0.1:$${AIRFLOW_PORT:-8081})
	docker compose up -d --build

down:
	docker compose down

airflow-test:
	docker compose exec airflow-scheduler python -m pytest tests/test_dag.py

spark-test:
	docker compose --profile spark run --rm spark python -m pytest tests/test_spark_jobs.py -v

# ==================== TESTS ====================
test:                     ## everything, incl. the ~4 min end-to-end test
	pytest

test-fast:
	pytest -m "not integration"

lint:
	ruff check .

# ==================== CLEAN ====================
clean:
	rm -rf data/*.parquet data/_manifest.json data/.staging data/bench data/spark
	rm -rf dbt_project/target/ dbt_project/logs/
	rm -rf __pycache__ */__pycache__
