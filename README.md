# ConnectHub Product Analytics Platform

**[Live Dashboard →](https://yourusername.github.io/connecthub-product-analytics/)**

End-to-end product analytics for a B2B SaaS communications platform — from raw event ingestion through a lakehouse architecture to interactive dashboards and an AI-powered analyst.

## See It Live

The live dashboard lets you experience the platform as a product team would — explore KPIs, retention cohorts, activation funnels, experiment results, customer health scores, and ask the AI Analyst any question about the data.

**[Open the Dashboard →](https://yourusername.github.io/connecthub-product-analytics/)**

## What This Demonstrates

- **Product Analytics**: activation funnels, retention cohorts, feature adoption curves, customer health scoring
- **Experimentation**: registry-driven deterministic assignment, power analysis, frequentist + Bayesian evaluation, guardrails, SRM checks
- **Data Engineering**: deterministic synthetic data → PostgreSQL bronze → incremental dbt (staging → intermediate → gold), orchestrated by Airflow
- **Data Quality**: Great Expectations contracts after every stage; critical failures stop the pipeline
- **Operations**: idempotent steps, structured JSON logs with run IDs, run history in `ops.pipeline_runs`, benchmarks
- **Visualization** (illustrative, unvalidated): Hex notebooks, LookML, Plotly

Not implemented: Kafka/Kinesis/S3 ingestion, Apache Iceberg, a serving API and the AI analyst. The dashboard in
`index.html` still shows static numbers. See [docs/architecture.md](docs/architecture.md).

## Architecture

```
generate (parquet) → assign (experiments.*) → ingest (bronze.*) → validate
    → dbt build (staging / intermediate / gold) → validate
    → analytics (health scores, retention, experiment results) → validate
```

Each arrow is one idempotent step of `python -m pipeline`; the Airflow DAG `connecthub_pipeline` runs the same steps.
Metric definitions: [docs/metric-definitions.md](docs/metric-definitions.md).

## Tech Stack

Python 3.11 | SQL | PostgreSQL 15 | dbt 1.7 | Apache Airflow 2.8 | Great Expectations 0.18 | pandas | NumPy | SciPy | scikit-learn | PySpark (isolated) | Docker

## Quick Start

### Requirements

- **Python 3.11** (pinned in `.python-version`). dbt-core 1.7 and Airflow 2.8 do not support Python 3.12+.
- Docker Desktop (PostgreSQL warehouse; Airflow and Spark images)

### Configuration

All connection settings and credentials come from environment variables. Nothing secret is committed.

```bash
cp .env.example .env            # then edit .env and set real passwords and keys
set -a; . ./.env; set +a        # bash: export the variables for dbt and the scripts
```

PowerShell:

```powershell
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v }
```

Use `POSTGRES_HOST=127.0.0.1`, not `localhost`: on Windows `localhost` tries IPv6 first and every connection then
waits about 2 seconds.

### Run the pipeline locally

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: py -3.11 -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt -c requirements/constraints-py311.txt

docker compose up -d postgres                 # warehouse on 127.0.0.1:5432
python -m pipeline run --users 10000          # all steps, about 1-2 minutes; safe to rerun
pytest                                        # unit, integration and end-to-end tests
```

Useful variants:

```bash
python -m pipeline run --steps dbt,validate_gold            # a subset of steps
python -m pipeline step ingest --start-date 2025-12-31      # load one day (incremental)
python -m pipeline verify-incremental                       # prove incremental dbt == full refresh
python -m pipeline fingerprint --out before.json            # compare warehouses with --compare
python scripts/benchmark.py --users 10000                   # timings and memory per step
```

### Command-line tools

| Command | What it does |
|---|---|
| `python -m experimentation.assignment [--persist]` | Assign users to every registered experiment |
| `python -m experimentation.evaluate --experiment-id exp_onboarding_v2` | Evaluate an experiment (`--all`, `--json`, `--persist`, `--fail-on-srm`) |
| `python -m analytics.cohort_engine --date 2025-12-31` | Retention as of a date (`--source parquet`, `--output cells.csv`) |
| `python -m analytics.health_scoring [--persist]` | Workspace health scores and tiers |
| `python -m quality.validate --stage all` | Run the data-quality checks (`bronze`, `gold`, `analytics` or `all`) |

All return `0` on success, `1` on errors or failed checks (with a message on stderr), and `2` on usage errors;
`evaluate --fail-on-srm` returns `3` when a sample ratio mismatch is found.

### With Docker: Airflow

```bash
docker compose up -d                          # postgres + airflow-init + webserver + scheduler
# UI: http://127.0.0.1:8081 (AIRFLOW_PORT), user/password from .env
docker compose exec airflow-scheduler airflow dags trigger connecthub_pipeline
docker compose exec airflow-scheduler python -m pytest tests/test_dag.py
```

The DAG accepts params `users`, `seed`, `partition_date` (incremental daily load) and `full_refresh`.

### Spark (optional, isolated)

```bash
docker compose --profile spark build spark
docker compose --profile spark run --rm spark python -m pytest tests/test_spark_jobs.py -v
```

Spark is not part of the pipeline; see [docs/architecture.md](docs/architecture.md#spark-isolated-not-in-the-pipeline).

### Synthetic data

`scripts/generate_synthetic_data.py` simulates a year (2025) of a B2B SaaS product with real behavioral structure,
so the analytics have signal to find:

- Workspaces have a latent engagement level that drives their users' behavior, plan changes, support load, AI
  agent quality and NPS.
- Users go through an ordered 14-day activation funnel (first call → AI feature → team invite); activity decays
  after signup and activated users churn later.
- Revenue is billed per active seat each month (`bronze.subscriptions`); NPS responses arrive at days 30/120/210/300.
- Experiments come from `experimentation/experiments.py`. `exp_onboarding_v2` has a **planted effect**: users
  assigned to `variant_1` are 1.3× as likely to use an AI feature after their first call. `exp_ai_summary_v1` is
  an A/A check with no effect.

The same `--seed` always produces identical files. Events are written in chunks, so memory stays bounded as
`--users` grows.

### Warehouse schemas

| Schema | Written by | Contents |
|---|---|---|
| `bronze` | ingestion | Raw events, users, workspaces, agent evaluations, subscriptions (MRR), NPS responses |
| `experiments` | assignment | `experiment_assignments` (one row per experiment and user) |
| `staging` | dbt | `stg_events` (incremental table) and typed views over bronze |
| `intermediate` | dbt | Sessions and feature usage (incremental), activation funnel |
| `gold` | dbt | Facts (incl. `fct_workspace_mrr`, `fct_experiment_user_metrics`) and `metrics_product_health` |
| `semantic` | dbt | MetricFlow time spine for the semantic layer |
| `analytics` | Python | `workspace_health_scores`, `experiment_results` |
| `ops` | pipeline | `pipeline_runs` (per-step status, metrics, timings), `load_state` |

### Dependencies

`requirements.txt` installs the development environment (`requirements/pipeline.txt` + `dev.txt`).
`requirements/constraints-py311.txt` pins every package, including transitive ones, to the versions validated on
Python 3.11.9; the Airflow image installs from the same file. Airflow itself uses its official constraints in its own
environment (Airflow 2.8 needs SQLAlchemy < 2.0, pandas 2.2 needs >= 2.0). Optional: `spark.txt` (Spark image),
`lookml.txt`.

## Project Structure

```
connecthub-product-analytics/
├── pipeline/                       # Step runner, CLI, logging, fingerprints, load state
├── scripts/                        # Data generation, ingestion, benchmark
├── experimentation/                # Experiment registry, assignment, statistics, evaluation
├── analytics/                      # Cohorts, funnel, adoption, health scoring
├── quality/                        # Data-quality contracts (Great Expectations)
├── dbt_project/                    # dbt models (staging → intermediate → gold) and tests
├── dags/                           # Airflow DAG
├── docker/                         # Airflow and Spark images
├── spark_jobs/                     # PySpark jobs (isolated, verified against dbt)
├── tests/                          # pytest: unit, integration, end-to-end
├── docs/                           # Architecture, metric definitions, playbooks
├── index.html                      # Static dashboard prototype (not connected to data)
└── hex_notebooks/, lookml/, notebooks/   # Illustrative analysis artefacts
```

## Key Modules

### Analytics
- **Cohort Engine** — Weekly retention matrix with heatmap visualization
- **Activation Funnel** — 4-stage funnel (Signup → Call → AI → Team)
- **Feature Adoption** — Cumulative adoption curves per feature over 90 days
- **Health Scoring** — Weighted composite score for churn prediction

### Experimentation
- **Power Analysis** — Sample size calculator for proportion and continuous metrics
- **Assignment** — Registry-driven, salted SHA-256 traffic and variant hashes; idempotent persistence
- **Statistical Tests** — Z-test for proportions, Welch's t-test, SRM check
- **Bayesian A/B** — Beta-Binomial model with probability of improvement
- **Evaluation** — End-to-end pipeline with guardrail metrics and decision logic

## License

MIT
