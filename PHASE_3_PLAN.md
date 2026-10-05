# Phase 3 Plan — Productionize the Analytics Pipeline

**Date:** 2026-10-04 · **Branch:** `phase-3` (from `phase-2` @ `d81f305`; `phase-2` is not merged into `main` yet)
**Out of scope:** frontend, FastAPI, AI Analyst, `index.html`.

## 0. What I found (inspection before any change)

| Area | Current state | Problem |
|---|---|---|
| dbt hot path | `stg_events` is a **view** (ROW_NUMBER dedupe over all events). `int_sessions`, `int_feature_usage`, `fct_daily_active_users`, `fct_retention_cohorts`, `fct_experiment_user_metrics` and `metrics_product_health` each re-run that dedupe over the full history. | Every run recomputes the whole event history, several times over. |
| **Baseline benchmark** (10K users, 783K events, `dbt run`, 13 models + 4 new) | 35.4–35.9 s wall. Top models: `int_sessions` ~10 s, `fct_daily_active_users` ~9.5 s, `int_feature_usage` ~8 s. | — |
| **Connection latency** (found while benchmarking) | `POSTGRES_HOST=localhost` resolves to `::1` first; Postgres is published on `127.0.0.1` only, so **every connection waits ~2 s** (measured 2.056 s vs 0.018 s). dbt opens one per model. | With `127.0.0.1`, the same baseline is 17.7 s. One run hit 55 s from Docker-VM contention, so benchmarks will report medians of repeated runs, and this fix is reported separately from the incremental gains. |
| Assignment | `variant = hash % n` and `bucket = hash % 10000` reuse **one** hash. No experiment registry or eligibility window. Persisted only via parquet, which ingestion **TRUNCATE**s and reloads. | Traffic and variant are correlated, there is no "who enters when", and assignments are not owned by the warehouse. |
| CLIs | `analytics.cohort_engine` and `experimentation.evaluate` have no argparse; the DAG passes `--date` to modules that ignore it. | DAG tasks would fail or do the wrong thing. |
| Airflow | Stock `apache/airflow:2.8.1`: no dbt, no project deps, no Java. Airflow metadata shares the **analytics DB**. No init service. Tasks use repo-relative paths. GE task runs a CLI with no data context. Spark tasks need Iceberg on S3. Port 8080 is taken by another project's Airflow on this machine. | The DAG cannot run at all. |
| Great Expectations | A JSON suite with 5 expectations and a checkpoint dict; no data context, no datasource, never executed. | Validation exists only on paper. |
| Spark | `sessionize_events.py` defaults to an Iceberg/S3 catalog; `--local` needs Java (none on the host). `feature_extraction.py` reads a file nothing produces. | Not executable here. |
| Observability | `print()` only. | No run ID, no metrics, no timings. |
| Config | Secrets only in `.env` (git-ignored) ✓. Only top-level dependencies are pinned. `localhost` default. No `AIRFLOW_PORT`. | Transitive dependencies not reproducible; the latency issue above. |
| Docs | `docs/architecture.md` describes Kafka/Kinesis/S3/Iceberg, which don't exist. `docs/metric-definitions.md` does not exist. | Docs overstate the system. |

**Machine constraints, measured:**
- Host: 7.7 GB RAM, 17 GB free disk.
- Docker VM: ~3.8 GB RAM, shared with 6 containers from other projects.
- The warehouse uses ~254 MB per 783K events, so **500K users (~40M events) projects to ~13 GB in bronze plus ~13 GB in materialized staging and intermediate tables.** That exceeds free disk. The 500K benchmark will run every stage that fits and report measured projections for the rest instead of pretending.

## 1. Incremental dbt

| Model | Now | Planned | Why |
|---|---|---|---|
| `stg_events` | view | **incremental** (delete+insert on `event_id`), indexed on `user_id`, `event_date`, `session_id` | Read by 7 models. Materializing it once removes the repeated dedupe. |
| `int_sessions` | table | **incremental** (`unique_key = session_id`) | Recompute only sessions that have events in the new window, from *all* their events, so a session crossing the window edge is still correct. |
| `int_feature_usage` | table | **incremental** (delete+insert on its grain) | Its grain includes `event_date`, so recomputing whole dates in the window is exact. |
| `stg_users`, `stg_workspaces`, `stg_subscriptions`, `stg_nps_responses` | view | view | Small, and full-replaced in bronze. |
| gold models, `int_activation_funnel` | table | table (reading the materialized upstreams) | Small outputs with global or rolling semantics; cheap once upstream is materialized. |

- **Window:** events with `event_date >= max(event_date in target) - var('lookback_days', 3)`. Late data older than the lookback needs `--full-refresh`.
- **Ingestion:** a partition mode (`--start-date/--end-date`) deletes and re-inserts that date range, so daily loads are idempotent and the incremental models have something to pick up.
- **Proof of correctness:**
  1. Load events through 2025-12-30, then full build.
  2. Ingest 2025-12-31 and run an incremental build.
  3. Separately `--full-refresh` the same data.
  4. **Fingerprint every model** (row count + md5 of ordered rows) and require them to be identical.
- Benchmark incremental vs full refresh at 10K and 100K users.

## 2. Experiment assignment

- **Registry** (`experimentation/experiments.py`): `id`, `start`/`end`, `traffic_pct`, `variants`, `salt`, eligibility.
  - `exp_onboarding_v2`: new signups in its window, assigned at signup. Keeps its planted effect.
  - `exp_ai_summary_v1`: an **A/A** check. 50% traffic, existing users assigned at their first activity on or after 2025-07-01; no effect planted, so evaluation must *not* ship it.
- **Two independent salted hashes:** `traffic` hash → in/out of the experiment; `variant` hash (salt) → arm.
- **Timing:** `assigned_at` comes from data (signup or first eligible activity), never from the clock.
- The generator uses the same registry, so planted effects apply exactly to assigned users. This changes the generated data, so all Phase 2 numbers will be regenerated. The seed stays 42, with no seed shopping.
- **Persistence:** `experiments.experiment_assignments` gets PK `(experiment_id, user_id)` and `INSERT … ON CONFLICT DO NOTHING`. Ingestion stops truncating it; the assignment step owns it.
- **Tests:** determinism, uniqueness, balance (SRM on large samples), persistence, rerun → no duplicates, multiple experiments independent, eligibility window and `assigned_at` correct.

## 3. CLIs

All three use argparse. Exit codes: `0` ok, `1` runtime or data error with a clear message, `2` usage error, `3` gate failed (only with `--fail-on-srm` / `--strict`).

| Command | Behavior |
|---|---|
| `python -m analytics.cohort_engine --date D [--weeks N] [--source warehouse\|parquet] [--output csv]` | Retention as of D |
| `python -m experimentation.evaluate --experiment-id X [--source …] [--json] [--fail-on-srm]` | Evaluate one experiment |
| `python -m analytics.health_scoring [--as-of D] [--source …] [--persist] [--output csv]` | Health scores |

Persisted outputs go to schema `analytics`: `workspace_health_scores` and `experiment_results`, replaced per snapshot so reruns don't duplicate. Tested by calling `main([...])`.

## 4. Pipeline package + Airflow

**`pipeline/` package.** One implementation is shared by local runs, Airflow and benchmarks. `python -m pipeline run|step <name>`; steps:
1. `generate`: skipped when `data/_manifest.json` already matches the parameters.
2. `assign`
3. `ingest`
4. `validate_bronze` (GE)
5. `dbt` (`dbt build`; failures fail the step)
6. `validate_gold` (GE)
7. `analytics`: cohort, health, evaluations
8. `validate_analytics` (GE)

**Airflow:**
- **Custom image:** `apache/airflow:2.8.1-python3.11` + project code + a **separate virtualenv** (`/opt/pipeline-venv`) with app, dbt and GE dependencies installed from a lock file. Airflow 2.8 needs SQLAlchemy < 2 and pandas 2.2 needs ≥ 2, so they can't share an environment.
- **No Java or Spark in the Airflow image:** Spark is not in the DAG (see §6).
- **Separate metadata DB:** an `airflow` database, created idempotently by an `airflow-init` service, which also runs migrations and creates the admin user from `.env`.
- **Ports and memory:** webserver on `${AIRFLOW_PORT:-8081}`, with reduced webserver workers for the ~3.8 GB VM.
- **DAG** `connecthub_pipeline`: one BashOperator per step, `cwd` set, Params for `users`/`seed`/`mode`, and every step idempotent.
- **Validation:** trigger it for real and confirm all tasks succeed; then trigger again to confirm reruns are safe.

## 5. Great Expectations (0.18.8)

Suites are defined in code (reviewable), run through an ephemeral context with a PostgreSQL SQL datasource. Each expectation is marked `critical` or `warning`; any critical failure exits non-zero and fails the pipeline.

| Stage | Expectations |
|---|---|
| **Bronze** | PK unique + not null on every table; compound PKs (subscriptions, assignments); `event_name` in the event catalog; dates within the data year; `first_active_date >= signup_date`; plan tiers; platforms; variants; `mrr_usd`/`seat_price_usd >= 0`; NPS 0–10; **orphan-FK checks** as query assets expected to return 0 rows (events→users, users→workspaces, NPS→users, evals→workspaces, assignments→users, subscriptions→workspaces) |
| **Gold** | retention and adoption as fractions in [0, 1] (stored as percent, checked as `/100`); `activated_14d ∈ {0,1}`; MRR ≥ 0; PKs |
| **Analytics** | `health_score` in [0, 100]; tiers in set; experiment results present with p-values in [0, 1] |

**Proof the checks can fail:** a test corrupts a copy of the data in a throwaway schema (duplicate PK, orphan event, bad tier, negative MRR) and asserts each corresponding expectation fails.

## 6. Spark — decision

**Spark is not necessary for this pipeline.** At the largest scale this machine supports, PostgreSQL + dbt (with §1) and the streaming generator handle the data; Spark adds a JVM and a second processing engine without adding capability. So:
- Keep it **isolated**: a separate `spark` image (python 3.11 + JRE 17 + pyspark 3.5.1), run with `docker compose --profile spark`. Not in the DAG.
- Make it **executable on the generated parquet** (Iceberg/S3 moved behind an explicit flag).
- **Validate:**
  - `feature_extraction` output must equal dbt `int_feature_usage` exactly.
  - 30-minute-gap sessionization must reproduce the generator's sessions (in-session gaps are minutes; sessions are hours apart).
- Integration test (`tests/test_spark_jobs.py`) executed **inside the spark container**, where Java exists.

## 7. Observability

- **Logging:** JSON-lines logs with `run_id`, `step`, `status`, `duration_s` and metrics (users, events generated, rows ingested per table, dbt models/tests executed and failed, validation pass/fail counts, experiment decisions, peak memory). Also a row per step in `ops.pipeline_runs`.
- **No secrets:** a redaction filter masks values of secret-named environment variables and connection URLs.
- **Errors:** failures log the step and the cause on one line and exit non-zero.

## 8. Idempotency

- `generate`: deterministic output with an atomic write.
- `assign`: `ON CONFLICT DO NOTHING`.
- `ingest`: full = truncate + load; partition = delete range + insert; each in one transaction.
- `dbt`: deterministic; incremental uses delete+insert.
- Analytics outputs: replaced per snapshot.

**Test:** an e2e integration test runs the full pipeline **twice in a separate database** (`<db>_e2e`) and requires identical fingerprints for every table. Run metadata (`ops.*`, timestamps) is excluded.

## 9. Configuration

- `.env.example`: placeholders only; `POSTGRES_HOST=127.0.0.1`; new `AIRFLOW_PORT` and `AIRFLOW_DB`.
- No hard-coded passwords anywhere; checked with a grep for secret values in tracked files.
- `requirements/constraints-py311.txt` is frozen from the validated venv and used by `make setup` and both images.
- Python 3.11 stays the supported version.

## 10. Benchmarks

`python -m pipeline bench` at 10K / 100K / 500K users records generation, ingestion, dbt (full vs incremental), validation, analytics, total time, and peak memory (Python per step; Postgres container from `docker stats`).
- **500K:** generation is measured. Ingestion and dbt run only if free disk allows, otherwise the projection is reported from measured per-row sizes.
- Medians of repeated runs where variance matters.

## 11–12. Validation and docs

- **End to end:** the full pipeline twice locally (fingerprints equal), then `pytest`, `ruff check .` and `dbt build`; the Airflow DAG triggered twice; the Spark test in its container.
- **Docs:**
  - `README.md`
  - `docs/architecture.md`: rewritten to describe what actually exists
  - `docs/metric-definitions.md`: new
  - `PHASE_3_REPORT.md`
