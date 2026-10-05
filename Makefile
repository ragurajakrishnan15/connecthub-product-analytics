.PHONY: setup test generate load-postgres run-docker stop-docker dbt-run dbt-test dbt-build spark-session spark-features run-experiments run-all lint clean

# Requires Python 3.11 (see .python-version). Configuration comes from the
# environment: copy .env.example to .env and export its variables first.

# ==================== SETUP ====================
setup:
	pip install -r requirements.txt
	@echo "✅ Dependencies installed"

# ==================== DATA ====================
generate:
	python scripts/generate_synthetic_data.py --users $(or $(USERS),10000) --seed $(or $(SEED),42)
	python -m experimentation.assignment
	@echo "✅ Synthetic data and experiment assignments generated in data/"

load-postgres:
	python scripts/ingest_events.py --load-postgres
	@echo "✅ Loaded data/ into bronze.* and experiments.*"

# ==================== DOCKER ====================
run-docker:
	docker compose up -d
	@echo "✅ Docker stack running"
	@echo "   Postgres: localhost:5432"
	@echo "   Airflow:  localhost:8080 (credentials from .env)"

stop-docker:
	docker compose down
	@echo "✅ Docker stack stopped"

# ==================== SPARK ====================
spark-session:
	spark-submit spark_jobs/sessionize_events.py --local
	@echo "✅ Events sessionized"

spark-features:
	spark-submit spark_jobs/feature_extraction.py data/events_sessionized.parquet data/feature_usage.parquet
	@echo "✅ Features extracted"

# ==================== DBT ====================
dbt-run:
	cd dbt_project && dbt run
	@echo "✅ dbt models built"

dbt-test:
	cd dbt_project && dbt test
	@echo "✅ dbt tests passed"

dbt-build:
	cd dbt_project && dbt build
	@echo "✅ dbt models built and tested"

# ==================== ANALYTICS ====================
run-experiments:
	python -m experimentation.evaluate
	@echo "✅ Experiments evaluated"

# ==================== TESTS ====================
test:
	pytest -v

lint:
	ruff check .
	@echo "✅ All tests passed"

# ==================== FULL PIPELINE ====================
run-all: generate spark-session spark-features run-experiments test
	@echo ""
	@echo "🚀 Full pipeline complete!"
	@echo "   - Synthetic data generated"
	@echo "   - Events sessionized"
	@echo "   - Features extracted"
	@echo "   - Experiments evaluated"
	@echo "   - Tests passed"

# ==================== CLEAN ====================
clean:
	rm -rf data/*.parquet
	rm -rf dbt_project/target/
	rm -rf __pycache__ */__pycache__
	@echo "✅ Cleaned"
