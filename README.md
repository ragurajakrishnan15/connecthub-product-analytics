# ConnectHub Product Analytics Platform

**[Live Dashboard →](https://yourusername.github.io/connecthub-product-analytics/)**

End-to-end product analytics for a B2B SaaS communications platform — from raw event ingestion through a lakehouse architecture to interactive dashboards and an AI-powered analyst.

## See It Live

The live dashboard lets you experience the platform as a product team would — explore KPIs, retention cohorts, activation funnels, experiment results, customer health scores, and ask the AI Analyst any question about the data.

**[Open the Dashboard →](https://yourusername.github.io/connecthub-product-analytics/)**

## What This Demonstrates

- **Product Analytics**: Activation funnels, retention cohorts, feature adoption curves, customer health scoring
- **Experimentation**: A/B testing with power analysis, frequentist + Bayesian evaluation, guardrail metrics
- **Data Engineering**: dbt models on PostgreSQL (bronze → staging → intermediate → gold), Airflow orchestration, PySpark at scale
- **Lakehouse Architecture**: Apache Iceberg tables with time-travel, schema evolution, partition pruning
- **Visualization**: Hex notebooks (SQL + Python), Looker dashboards with LookML semantic layer, Plotly charts
- **Data Quality**: Great Expectations contracts between pipeline stages
- **AI-Powered Insights**: Interactive AI analyst that answers questions about the product data

## Architecture

```
Ingestion (Kafka/Kinesis/S3)
    → Processing (PySpark/Airflow)
    → Lakehouse (Iceberg/dbt)
    → Analytics (Cohort/Experiment engines)
    → Serving (Hex/Looker/Dashboard)
```

## Tech Stack

Python | SQL | PySpark | dbt | Apache Airflow | Apache Iceberg | Hex | Looker (LookML) | Plotly | Chart.js | Great Expectations | scikit-learn | Pandas | NumPy | SciPy

## Quick Start

### Requirements

- **Python 3.11** (pinned in `.python-version`). dbt-core 1.7 and Airflow 2.8 do not support Python 3.12+.
- Docker Desktop (for the local PostgreSQL warehouse)
- Optional: Java 17 for the PySpark jobs (`requirements/spark.txt`)

### Configuration

All connection settings and credentials come from environment variables. Nothing secret is committed.

```bash
cp .env.example .env            # then edit .env and set real passwords
set -a; . ./.env; set +a        # bash: export the variables for dbt and the scripts
```

PowerShell:

```powershell
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v }
```

### Run the pipeline locally

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: py -3.11 -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt

pytest                                                   # unit + PostgreSQL integration tests
docker compose up -d postgres                            # local warehouse on 127.0.0.1:5432
python scripts/generate_synthetic_data.py --users 2000 --workspaces 200 --evaluations 2000
python -m experimentation.assignment                     # writes data/experiment_assignments.parquet
python scripts/ingest_events.py --load-postgres          # loads bronze.* and experiments.*
cd dbt_project && dbt build && cd ..                     # builds staging / intermediate / gold / semantic
```

The generator's defaults (500K users) need tens of GB of RAM; use the flags above for local development.

### Warehouse schemas

| Schema | Written by | Contents |
|---|---|---|
| `bronze` | `scripts/ingest_events.py` | Raw events, users, workspaces, agent evaluations |
| `experiments` | `scripts/ingest_events.py` | `experiment_assignments` |
| `staging` | dbt | Typed, deduplicated views over bronze |
| `intermediate` | dbt | Sessions, feature usage, activation funnel |
| `gold` | dbt | Facts and `metrics_product_health` consumed by Python, LookML and Hex |
| `semantic` | dbt | MetricFlow time spine for the semantic layer |

### Dependencies

`requirements.txt` installs the development environment (`requirements/app.txt`, `dbt.txt`, `dev.txt`).
Optional sets live beside them: `spark.txt`, `airflow.txt` (container/separate venv only: Airflow 2.8 needs SQLAlchemy < 2.0, pandas 2.2 needs >= 2.0), and `quality.txt`.

### With Docker (full stack):

```bash
docker compose up -d    # Postgres + Airflow; Airflow UI on localhost:8080, credentials from .env
```

## Project Structure

```
connecthub-product-analytics/
├── index.html                      # Live interactive dashboard with AI analyst
├── scripts/                        # Data generation & ingestion
├── spark_jobs/                     # PySpark pipelines
├── dbt_project/                    # dbt models (staging → intermediate → gold)
├── dags/                           # Airflow DAGs
├── great_expectations/             # Data quality checks
├── analytics/                      # Product analytics modules
├── experimentation/                # A/B testing framework
├── hex_notebooks/                  # Hex notebook configs
├── lookml/                         # Looker semantic layer (LookML)
├── notebooks/                      # Jupyter exploration notebooks
├── tests/                          # Unit tests (pytest)
└── docs/                           # Architecture docs & playbooks
```

## Key Modules

### Analytics
- **Cohort Engine** — Weekly retention matrix with heatmap visualization
- **Activation Funnel** — 4-stage funnel (Signup → Call → AI → Team)
- **Feature Adoption** — Cumulative adoption curves per feature over 90 days
- **Health Scoring** — Weighted composite score for churn prediction

### Experimentation
- **Power Analysis** — Sample size calculator for proportion and continuous metrics
- **Assignment** — Deterministic SHA-256 hashing for consistent variant assignment
- **Statistical Tests** — Z-test for proportions, Welch's t-test, SRM check
- **Bayesian A/B** — Beta-Binomial model with probability of improvement
- **Evaluation** — End-to-end pipeline with guardrail metrics and decision logic

## License

MIT
