# ConnectHub Product Analytics Platform

End-to-end product analytics for a B2B SaaS communications platform — from deterministic synthetic event data through a PostgreSQL warehouse and incremental dbt models to a read-only analytics API and an interactive dashboard.

## The Dashboard

`index.html` is a single-page dashboard that the analytics API serves from its own origin. **Every number on it comes from the API**, which reads the warehouse: nothing is hard-coded, and each panel shows its own loading, empty or error state. It needs a built warehouse and a running API (see [Quick Start](#quick-start)), so there is no hosted demo: a static host such as GitHub Pages cannot serve it.

| Tab | What it shows |
|---|---|
| Overview | KPI cards, daily active users, feature adoption, revenue by plan, AI-agent resolution rate, and a Voice of Customer section (NPS, support tickets, CSAT) |
| Retention | Weekly cohort retention matrix |
| Activation | 14-day activation funnel and time to each milestone |
| Experiments | A selector over the registered experiments, with lift, Bayesian probability, SRM and guardrail checks, the verdict and the conversion curve (the A/A check is labelled as one) |
| Customer Health | Health tier counts, score distribution and the lowest-scoring workspaces |
| AI Analyst | A chat over `POST /api/analyst/chat`: answers come only from the dashboard's own API data, every number is checked against the data it came from (an answer with a figure that cannot be matched is withheld), and each answer lists its sources. Off by default; the tab says "not configured" until it is set up (see [AI analyst](#ai-analyst)) |

The data is synthetic (the page says so), and the numbers are small by design: the default dataset is 10,000 users.
See [docs/api.md](docs/api.md#dashboard-page-get-) for how the page is served and what each panel reads.

## What This Demonstrates

- **Product Analytics**: activation funnels, retention cohorts, feature adoption curves, customer health scoring
- **Experimentation**: registry-driven deterministic assignment, power analysis, frequentist + Bayesian evaluation, guardrails, SRM checks
- **Data Engineering**: deterministic synthetic data → PostgreSQL bronze → incremental dbt (staging → intermediate → gold), orchestrated by Airflow
- **Data Quality**: Great Expectations contracts after every stage; critical failures stop the pipeline
- **Operations**: idempotent steps, structured JSON logs with run IDs, run history in `ops.pipeline_runs`, benchmarks
- **Serving**: a read-only FastAPI service (dedicated database role, bound parameters, per-data-version cache with ETags, a pinned OpenAPI contract) and a dashboard that renders only what the API returns
- **Secure front end**: same-origin serving, a Content-Security-Policy derived from the page (no `unsafe-inline`, no `unsafe-eval`, no CDN for scripts: Chart.js is vendored and integrity-checked), API text written to the page as text only, an optional API key kept in `sessionStorage`
- **Visualization** (illustrative, unvalidated): Hex notebooks, LookML, Plotly

The API (`api/`) is documented in [docs/api.md](docs/api.md); the dashboard is part of the same service.

Not implemented: Kafka/Kinesis/S3 ingestion and Apache Iceberg.
See [docs/architecture.md](docs/architecture.md).

## AI analyst

**Status: the implementation is complete; the real Gemini live evaluation is incomplete** (provider quota/rate limiting stopped it). The server-side code, the tab and every keyless and mocked test are done; what was *not* proven is the analyst's behaviour on the full set of live Gemini questions. Details, numbers and the exact gap are in [PHASE_6_REPORT.md](PHASE_6_REPORT.md).

The browser never sees a model key. `POST /api/analyst/chat` runs the question through a server-side engine that may call only 14 read-only tools (thin wrappers over the existing API services, on the read-only database role), then checks every number in the answer against what those tools returned. Off by default: nothing is sent anywhere unless all of the following are set **in the process environment** of the API (a `.env` file is never read by the API):

```powershell
$env:ANALYST_ENABLED = 'true'
$env:ANALYST_MODEL   = '<a current Gemini model id>'   # no default; chosen by you
$env:GEMINI_API_KEY  = '...'                            # never put it in a file, an image or the page
pip install -r requirements/analyst.txt -c requirements/constraints-py311.txt   # the optional SDK
python -m api
```

The page's address must be in `API_CORS_ORIGINS` (default `127.0.0.1:8000` and `localhost:8000`) because the route refuses cross-origin requests. Limits, errors and the response shape are in [docs/api.md](docs/api.md#post-apianalystchat). The Docker image does not include the SDK and `docker-compose.yml` does not pass the analyst settings, so the analyst answers "not configured" in the container (a documented gap, see the report).

The live harness `scripts/analyst_live_eval.py` is the only code that may call Gemini; it refuses to run without `--approve-live`, and no test can contact Google (`tests/conftest.py`).

## Architecture

```
generate (parquet) → assign (experiments.*) → ingest (bronze.*) → validate
    → dbt build (staging / intermediate / gold) → validate
    → analytics (health scores, retention, experiment results) → validate

warehouse (gold, analytics, ops.pipeline_runs) → api/ (FastAPI, read-only role) → dashboard in the browser (same origin)
```

Each arrow of the pipeline is one idempotent step of `python -m pipeline`; the Airflow DAG `connecthub_pipeline` runs the
same steps. The API and the dashboard only read what the pipeline built: the browser talks to the API and never to the
database.
Metric definitions: [docs/metric-definitions.md](docs/metric-definitions.md).

## Tech Stack

Python 3.11 | SQL | PostgreSQL 15 | dbt 1.7 | Apache Airflow 2.8 | Great Expectations 0.18 | FastAPI | pandas | NumPy | SciPy | scikit-learn | PySpark (isolated) | Docker | Chart.js 4.4.1 (vendored; plain HTML and JavaScript, no build step) | Node.js and Playwright (dashboard tests only)

## Quick Start

### Requirements

- **Python 3.11** (pinned in `.python-version`). dbt-core 1.7 and Airflow 2.8 do not support Python 3.12+.
- Docker Desktop (PostgreSQL warehouse, the API and dashboard; Airflow and Spark images)
- Node.js, optional: only the dashboard's JavaScript tests need it (`tests/dashboard`, run by `pytest`; developed with Node 24). Without Node that one test is skipped.
- Playwright and Chromium, optional: only the dashboard's browser tests (`tests/browser`) need them.
  `pip install -r requirements/browser.txt -c requirements/constraints-py311.txt`; the browser itself is downloaded
  separately (see that file, which also shows how to keep it off a nearly full drive). Without them those tests are
  skipped.

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

### Analytics API and dashboard

```bash
python -m api.provision                     # once: read-only database role (set API_DB_PASSWORD in .env)
docker compose up -d postgres api-init api  # API and dashboard in Docker
```

Open **http://127.0.0.1:8000/** for the dashboard and `/api/docs` for the interactive API reference. The port is
`API_PORT` in `.env`; set it to something else if 8000 is taken. Run the pipeline first: until it has built and
validated the warehouse, each dashboard panel shows "Warehouse not ready" with a Retry button.

Without Docker, run the API from the repository root and tell it which page to serve:

```bash
API_DASHBOARD_PATH=index.html python -m api                 # bash
$env:API_DASHBOARD_PATH = 'index.html'; python -m api       # PowerShell
```

`API_DASHBOARD_PATH` is unset by default, in which case the API serves no page (the compose service sets it for you).
The page must be opened through the API: opened as a file, its panels show an error. If the API runs with
`API_AUTH_MODE=api_key`, the page asks for a key when the API answers 401 and keeps it for that browser tab only.
The dashboard loads Chart.js from this repository (`vendor/`), not from a CDN; only the page's fonts still come from
Google Fonts.

Endpoints: `/api/health`, `/api/health/ready`, `/api/meta`, `/api/overview`, `/api/engagement`, `/api/activation`,
`/api/retention`, `/api/cohorts`, `/api/revenue`, `/api/feature-adoption`, `/api/experiments[/{id}]`, `/api/nps`,
`/api/support`, `/api/customer-health[/workspaces]`. Reference: [docs/api.md](docs/api.md).

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
├── api/                            # Read-only analytics API (FastAPI): routers, services, repositories
├── pipeline/                       # Step runner, CLI, logging, fingerprints, load state
├── scripts/                        # Data generation, ingestion, benchmark
├── experimentation/                # Experiment registry, assignment, statistics, evaluation
├── analytics/                      # Cohorts, funnel, adoption, health scoring
├── quality/                        # Data-quality contracts (Great Expectations)
├── dbt_project/                    # dbt models (staging → intermediate → gold) and tests
├── dags/                           # Airflow DAG
├── docker/                         # Airflow and Spark images
├── spark_jobs/                     # PySpark jobs (isolated, verified against dbt)
├── tests/                          # pytest: unit, integration, end-to-end; tests/dashboard: Node tests, tests/browser: Playwright
├── docs/                           # Architecture, API reference, metric definitions, playbooks
├── index.html                      # The dashboard: one file, served by the API at / (reads only the API)
├── vendor/                         # Vendored Chart.js 4.4.1 (MIT), its license and provenance
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

### Serving
- **Analytics API** (`api/`) — Read-only endpoints over the gold serving tables, with caching, ETags and a committed OpenAPI contract ([docs/api.md](docs/api.md))
- **Dashboard** (`index.html`) — One file in three layers: a data layer (same-origin requests, ETag revalidation, error and key handling), pure panel models (API data to display), and panels with loading, empty and error states. Tested in Node (`tests/dashboard`) and in Chromium with Playwright against the real API and warehouse (`tests/browser`)
- **Dashboard serving** (`api/dashboard.py`) — Reads the page at startup, derives its Content-Security-Policy from it, and refuses to start on inline handlers, remote scripts or a vendored script whose hash does not match

## License

MIT. The vendored Chart.js in `vendor/` is MIT too (see `vendor/LICENSE-chartjs.md`).
