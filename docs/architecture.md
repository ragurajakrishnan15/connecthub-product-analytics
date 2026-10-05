# ConnectHub Analytics Architecture

This describes what is implemented and runs today. Earlier versions of this
page described Kafka/Kinesis/S3/Iceberg ingestion; none of that exists, and it
is listed under "Not implemented" below.

## Data flow

```
scripts/generate_synthetic_data.py      deterministic synthetic product data (parquet)
        │  data/*.parquet + data/_manifest.json
        ▼
experimentation/assignment.py           experiment registry -> experiments.experiment_assignments
        ▼
scripts/ingest_events.py                parquet -> bronze.* (PostgreSQL COPY; full or per-day partition)
        ▼
quality/ (Great Expectations)           validate bronze ── critical failure stops the pipeline
        ▼
dbt_project/ (dbt-postgres)             staging -> intermediate -> gold (+ semantic)
        ▼
quality/                                validate gold
        ▼
analytics/, experimentation/evaluate.py health scores, retention summary, experiment results
        │  analytics.workspace_health_scores, analytics.experiment_results
        ▼
quality/                                validate analytics outputs
```

Each box is one **pipeline step** (`pipeline/steps.py`), run the same way
everywhere:

| Where | How |
|---|---|
| Local | `python -m pipeline run` (each step in its own process) or `python -m pipeline step <name>` |
| Airflow | DAG `connecthub_pipeline` (`dags/connecthub_pipeline.py`): one BashOperator per step running `$PIPELINE_PYTHON -m pipeline step <name>` |
| Benchmarks / tests | `scripts/benchmark.py`, `tests/test_e2e_pipeline.py` call the same CLI |

## Warehouse (PostgreSQL 15)

| Schema | Written by | Contents |
|---|---|---|
| `bronze` | ingestion | raw events, users, workspaces, agent evaluations, subscriptions, NPS |
| `experiments` | assignment | `experiment_assignments` (PK experiment_id, user_id) |
| `staging` | dbt | `stg_events` (incremental table), dimension views |
| `intermediate` | dbt | `int_sessions`, `int_feature_usage` (incremental), `int_activation_funnel` |
| `gold` | dbt | facts and `metrics_product_health`; serving models (`models/gold/serving/`, tag `serving`): small additive daily/monthly tables for the API, see [metric-definitions](metric-definitions.md#serving-models-read-by-the-api) |
| `semantic` | dbt | MetricFlow time spine |
| `analytics` | Python analytics | `workspace_health_scores`, `experiment_results` |
| `ops` | pipeline | `pipeline_runs` (one row per run x step, with metrics), `load_state` |

Airflow's own metadata lives in a separate database (`airflow`), not in the warehouse.

## Incremental processing

`stg_events`, `int_sessions` and `int_feature_usage` are incremental
(`delete+insert`), as are the serving models that read events
(`fct_support_daily` by date; `fct_activity_monthly` and
`fct_feature_usage_monthly` by whole month). Each run reprocesses events dated within `lookback_days`
(default 3, `dbt_project.yml`) of the latest date already built:

- `stg_events` / `int_feature_usage`: whole dates in the window are recomputed.
- `int_sessions`: every session with an event in the window is recomputed from
  all of its events, so sessions crossing the window edge stay exact.

Daily loads use partition ingestion (`--start-date D`), which replaces that
day in bronze. A **full** reload of different data (new seed, new scale) is
recorded in `ops.load_state`; the next dbt step then runs `--full-refresh`
automatically. Reloading the same dataset is a no-op. Correctness is checked by
`python -m pipeline verify-incremental`, which compares fingerprints of every
relation after an incremental build and after a full refresh of the same data.

## Idempotency

| Step | Rerun behaviour |
|---|---|
| generate | skipped when `data/_manifest.json` matches the parameters and generator code; otherwise written to a staging dir and moved into place, manifest last |
| assign | deterministic; `INSERT ... ON CONFLICT DO NOTHING` (a user's first assignment sticks) |
| ingest | full: TRUNCATE + COPY in one transaction (skipped if the same dataset is loaded); partition: DELETE range + COPY in one transaction |
| dbt | deterministic models; incremental `delete+insert` |
| analytics | health scores and experiment results replaced per snapshot / experiment |

`tests/test_e2e_pipeline.py` runs the full pipeline twice in a separate
database and requires identical fingerprints for every relation.

## Data quality

`quality/expectations.py` defines the contracts; `quality/validate.py` runs them
with Great Expectations 0.18 (ephemeral context, PostgreSQL datasource, nothing
written to disk). Checks cover primary keys, not-null columns, valid event types,
dates, plan tiers, experiment variants, non-negative revenue, rates in [0, 1],
health scores in [0, 100], orphan foreign keys and SRM. Each check is `critical`
(fails the step, and therefore the Airflow run) or `warning` (reported).
dbt tests (57) run inside `dbt build` as a second layer.

## Orchestration

`docker/airflow/Dockerfile` extends `apache/airflow:2.8.1-python3.11` with the
project code and a **separate virtualenv** for the pipeline: Airflow 2.8 pins
SQLAlchemy < 2 while pandas 2.2 needs >= 2, so they cannot share an environment.
Both install from pinned files (`requirements/constraints-py311.txt` for the
pipeline, Airflow's official constraints for Airflow). `airflow-init` creates the
metadata DB, migrates it and creates the admin user, idempotently. Credentials
come from `.env` via compose; the web UI binds to `127.0.0.1:${AIRFLOW_PORT}`.

## Spark: isolated, not in the pipeline

`spark_jobs/` (sessionization with a 30-minute gap, daily feature extraction)
runs in its own image (`docker/spark`, `docker compose --profile spark`). It is
**not** part of the pipeline because it adds nothing the pipeline needs at the
scales this project runs: the generator streams parquet in chunks, PostgreSQL +
incremental dbt process 100K users (8M events) on a laptop, and Spark would add
a JVM and a second processing engine. The jobs are kept executable and verified
(`tests/test_spark_jobs.py`, run in the Spark container): feature extraction
matches dbt's `int_feature_usage` row for row, and gap-based sessionization
recovers the generated sessions. Spark becomes worth adding if event volume
outgrows one PostgreSQL instance; the jobs are the starting point for that.

## Observability

Every step logs JSON lines to stdout: `run_id`, `step`, `event`, duration, peak
memory and step metrics (rows generated / ingested, dbt models and tests,
validation results, experiment decisions). The same metrics are stored in
`ops.pipeline_runs`. Secret values from the environment and passwords in
connection URLs are masked before anything is written (`pipeline/log.py`).

## Not implemented

Kafka / Kinesis / S3 ingestion, Apache Iceberg, a serving API, the live
dashboard's data connection and the AI analyst. `index.html` still shows static
numbers. LookML and Hex files are illustrative and unvalidated.
