# Phase 3 Report: Productionize the Analytics Pipeline

**Date:** 2026-10-05 · **Branch:** `phase-3` (from `phase-2` @ `d81f305`) · **Plan:** `PHASE_3_PLAN.md`
**Environment:**
- Host: Windows 11, 7.7 GB RAM, 6–17 GB free disk during the work
- Docker Desktop VM: ~3.8 GB RAM, shared with 6 containers from other projects
- Python 3.11.9 (`.venv`); PostgreSQL 15
**Out of scope and untouched:** frontend, FastAPI, the AI analyst, `index.html`.

## Bottom line

| Check | Result |
|---|---|
| `pytest` (dev venv) | **96 passed**, 0 failed. The 2 skips are the DAG and Spark tests, which need their containers and passed there. |
| DAG tests (Airflow container) | **3 passed** |
| Spark tests (Spark container) | **4 passed** |
| `ruff check .` | All checks passed |
| `dbt build` | **PASS=74**: 17 models (3 incremental) + 57 tests |
| Pipeline run twice locally | Both succeed; **identical fingerprints** across 26 relations (2,093,729 rows) |
| Airflow DAG run twice (`manual_run_1`, `manual_run_2`) | **All 8 tasks succeeded both times; identical fingerprints** |
| Incremental dbt vs full refresh, same data | **Identical** on all 26 relations, locally and in the e2e test's separate database |
| Data quality (Great Expectations) | **89/89 expectations pass** on clean data. Corrupted data fails 5 targeted checks and stops the pipeline. |
| Secrets | No secret value in any of 121 tracked/new files or 18 Airflow log files; `.env` is git-ignored |
| 100K / 500K benchmarks | **Partial.** Generation was measured at both scales. The 100K run was **stopped by the host's low-memory protection** during ingestion; 500K ingestion/dbt **don't fit on this machine's disk**. See §10. |

---

## 1. Incremental dbt

**Before:** `stg_events` was a view, so 6 downstream models re-ran its dedupe over all events on every build.

| Model | Before | After |
|---|---|---|
| `stg_events` | view | **incremental table** (`delete+insert` on `event_id`), indexes on `event_id` (unique), `user_id`, `event_date`, `session_id` |
| `int_sessions` | table | **incremental** (`session_id`). Recomputes every session that has an event in the window, from *all* of its events. |
| `int_feature_usage` | table | **incremental** (grain key). Recomputes whole dates in the window. |
| `stg_users`, `stg_workspaces`, `stg_subscriptions`, `stg_nps_responses` | view | view (small; fully replaced in bronze) |
| gold, `int_activation_funnel` | table | table, now reading materialized upstreams |

- **Window:** `event_date >= max(date already built) - var('lookback_days', 3)` (`macros/incremental_window.sql`).
- **Ingestion modes:** a per-day partition mode (DELETE range + COPY in one transaction) feeds the incremental runs.
- **Correctness guard:** incremental models are only correct if older bronze events didn't change. `ops.load_state` records which dataset was loaded:
  - A full reload of *different* data sets `needs_full_refresh`, which the dbt step honors and then clears.
  - Reloading the *same* data is skipped.
  - This closed a real hole I found during testing: a full reload followed by an incremental run would have kept stale events.
- **Verification:** `python -m pipeline verify-incremental` loads events through Dec 30 and builds, adds Dec 31 and builds incrementally, then full-refreshes the same data. Fingerprints (row count + order-independent md5 of every row) were **identical for all 26 relations**.
- **Benchmark:** §10.

**Baseline before any change** (10K users, `dbt run`):
- 35.4 s and 35.9 s with `POSTGRES_HOST=localhost`.
- 17.7 s with `127.0.0.1`, plus one outlier at 55.2 s from VM contention.

**Connection fix:** on Windows `localhost` tries IPv6 first; Postgres is published on IPv4 only, so **every connection waited ~2 s** (measured 2.056 s vs 0.018 s). dbt opens one connection per model. The default is now `127.0.0.1` in code and in `.env.example`.

## 2. Experiment assignment

- **Registry** (`experimentation/experiments.py`), the single source used by assignment, the generator (planted effects) and the pipeline:
  - `exp_onboarding_v2`: new signups, 100% traffic, assigned at signup; carries the planted effect.
  - `exp_ai_summary_v1`: an **A/A check**. Users active on or after 2025-07-01, 50% traffic, assigned at their first activity in the window; no effect.
- **Two independent salted SHA-256 hashes:**
  - `experiment:traffic:user` → in or out of the experiment.
  - `experiment:variant:salt:user` → arm.
  - Changing the salt reshuffles arms without changing who is in (tested).
- **No clock:** `assigned_at` always comes from data.
- **Warehouse table:** `experiments.experiment_assignments` gets `PRIMARY KEY (experiment_id, user_id)` and is filled through a temp table with `INSERT … ON CONFLICT DO NOTHING`.
  - Reruns insert 0 rows, and a user's first assignment always sticks (tested with deliberately flipped variants).
  - Ingestion no longer truncates the table; the old key-less table is migrated automatically.
- **Tests (18 in `test_assignment.py`):**
  - Determinism and uniqueness.
  - Balance: SRM p > 0.001 on 100K IDs and within 1% of 50/50.
  - Traffic/variant independence; salt behavior.
  - Eligibility windows, and `assigned_at` = signup or first activity after the start date.
  - Holdout exclusion and multiple-experiment independence (χ² on the crosstab).
  - Persistence and idempotency (integration).
  - A/A calibration.
- **Balance on the real data** (seed 42, unchanged; no seed shopping):
  - `exp_onboarding_v2`: 4,966 / 5,034 (SRM p = 0.50).
  - `exp_ai_summary_v1`: 1,947 / 2,011 (SRM p = 0.31).

## 3. CLIs

All use argparse, print human-readable output (or `--json`), and exit `0` ok / `1` error or no data / `2` usage / `3` SRM gate.

| Command | Notes |
|---|---|
| `python -m analytics.cohort_engine --date D [--weeks N] [--source warehouse\|parquet] [--output csv] [--json]` | Retention as of D; only weeks fully ended by D count |
| `python -m experimentation.evaluate --experiment-id X \| --all [--source …] [--persist] [--json] [--fail-on-srm]` | Also accepts `--date` for scheduler compatibility |
| `python -m analytics.health_scoring [--as-of D --source parquet] [--persist] [--output csv]` | |
| `python -m experimentation.assignment [--experiment-id X] [--persist]` | |
| `python -m quality.validate --stage bronze\|gold\|analytics\|all` | |
| `python -m pipeline run\|step\|fingerprint\|verify-incremental` | |

`tests/test_cli.py` (16 tests) covers success output, JSON/CSV output, no-data exit 1, missing files, unknown experiment and usage errors (exit 2).

## 4. Airflow

**What was broken:**
- The stock image had no dbt or project dependencies.
- The DAG called CLIs that didn't exist and used repo-relative paths.
- Spark tasks needed Iceberg on S3, and the Great Expectations task had no context.
- Airflow metadata lived in the analytics database.
- There was no init service, and port 8080 is taken by another project's Airflow on this machine.

**Now:**
- **Custom image** (`docker/airflow/Dockerfile`, 3.39 GB): `apache/airflow:2.8.1-python3.11` + project code + a **separate pipeline virtualenv** installed from the lock file (`pip check` clean). Airflow 2.8 needs SQLAlchemy < 2 and pandas 2.2 needs ≥ 2, so they can't share an environment. No Java or Spark (§6).
- **`airflow-init`:** creates a separate `airflow` metadata DB, migrates it and creates the admin user from `.env`, all idempotently. The entrypoint's DB wait is disabled for this one service, because this service is what creates that DB.
- **DAG `connecthub_pipeline`:**
  - Tasks: `generate → assign → ingest → validate_bronze → dbt → validate_gold → analytics → validate_analytics`, each running `$PIPELINE_PYTHON -m pipeline step <name> --run-id {{ run_id }}`.
  - Params: `users`, `seed`, `partition_date`, `full_refresh`.
  - Settings: `max_active_runs=1`, retries 1, a timeout per task.
  - UI on `127.0.0.1:${AIRFLOW_PORT:-8081}`.

**Runs:**
1. **The first (scheduled) run failed**, and failure handling worked: dbt failed and downstream tasks were marked `upstream_failed`.
   - Cause: dbt-core 1.7.4 fires a resource-report event through a protobuf API removed in protobuf 5.26. It only fires on POSIX, which is why the Windows host never hit it.
   - Fix: pinned `protobuf>=4.21,<5` in `requirements/dbt.txt` and the lock file.
2. **`manual_run_1` succeeded on all 8 tasks.** dbt did a full refresh, because the failed run had left `needs_full_refresh` set.
3. **`manual_run_2` succeeded on all 8 tasks.**
   - Generate skipped, 0 assignments inserted, event reload skipped.
   - dbt ran **incrementally in 23.8 s vs 84.5 s**.
   - **Warehouse fingerprint identical to run 1.**
4. The DAG test (`tests/test_dag.py`, in the container: import errors, task chain, no secrets in commands) caught a real templating bug before the first run (`str.format` vs Jinja braces).

## 5. Great Expectations

- The placeholders were removed: an unused JSON suite and a checkpoint dict that never ran.
- Contracts are now defined in code (`quality/expectations.py`) and run by `quality/validate.py`: GE 0.18.8, ephemeral context, PostgreSQL datasource, nothing written to disk, critical/warning severity, exit 1 on any critical failure.

| Stage | Expectations | Covers |
|---|---|---|
| bronze | 52 | PK not-null + no duplicates on every table (compound for subscriptions and assignments); event types; platforms; dates within 2025; plan tiers; seat_count ≥ 1; MRR, seat price, billed seats ≥ 0; NPS 0–10; CSAT 1–5; variants and experiment IDs; **7 orphan-FK checks**; events before signup; timestamp/date mismatch; first_active < signup; assignment before signup |
| gold | 24 | retention and adoption **as fractions in [0, 1]**; active ≤ cohort size; DAU ≤ WAU ≤ MAU; experiment metrics PK; `activated_14d ∈ {0,1}`; revenue ≥ 0; health inputs in range; orphan checks |
| analytics | 13 | health score in [0, 100], not null, valid tiers, one row per workspace; experiment results present for every assigned experiment, p-values in [0, 1]; **no SRM** |

**Results:**
- **Clean data:** 52/52, 24/24 and 13/13 pass.
- **Corrupted data** (e2e test: duplicate event, orphan event with an invalid type, invalid plan tier, negative MRR): each targeted check fails, and `pipeline step validate_bronze` exits 1 with `bronze data-quality checks failed: …`.
- **Performance finding:** GE's built-in `expect_column_values_to_be_unique` took **334 s** on 781K events (its SQL is `NOT IN` over a grouped subquery). I replaced it with an equivalent duplicate-key query asset. **The bronze stage went from 397 s to 11.7 s.**

## 6. Spark

**Decision: not necessary for this pipeline; kept isolated and verified.**
- **Why not needed:** the generator streams parquet in chunks (500K users in 168 s, 1.5 GB), and PostgreSQL + incremental dbt handle the data sizes this project actually runs.
- **Isolated:** Spark is its own image (`docker/spark`: python 3.11 on bookworm + OpenJDK 17 + pyspark 3.5.1; 1.64 GB) behind `docker compose --profile spark`, and is not in the DAG.
- **Executable:** jobs run on the generated parquet via `spark-submit`. Iceberg/S3 moved behind an explicit `--iceberg` flag. The shared feature map comes from `analytics/features.py`.
- **Verified** (4 tests in the container):
  - `feature_extraction` output equals dbt `int_feature_usage` **row for row (248,755 rows)** and a pandas reference.
  - 30-minute-gap sessionization never merges two generated sessions (100% pure). It produces 147,566 sessions vs 146,921 generated, splitting 0.44% where an in-session gap exceeds 30 minutes.
  - The gap boundary unit test passes.
- **Build fix:** the `python:3.11-slim` tag now points to Debian trixie, which has no OpenJDK 17, so the base is pinned to bookworm.

## 7. Observability

- **JSON logs:** every step logs lines with `run_id`, `step`, `event`, `duration_s`, `peak_memory_mb` and metrics:
  - users and events generated; assignments inserted vs present, with SRM p-values
  - rows ingested per table and the load mode
  - dbt models (incremental count), tests passed, failed nodes, slowest models
  - validation pass/fail lists; health tiers; retention summary; experiment decisions
- **Run history:** the same data goes into `ops.pipeline_runs`, one row per run × step (status, duration, peak memory, metrics JSONB, error).
- **Failures:** logged as `step.failed` with the cause, plus `error: step X failed: …` on stderr and exit 1.
- **Redaction:** secret environment values and passwords in URLs are masked (`tests/test_pipeline_log.py`).

## 8. Idempotency

- **Explicit test:** the e2e test (`tests/test_e2e_pipeline.py`, ~3 min, separate database `<db>_e2e`) runs the full pipeline twice and requires:
  - **identical fingerprints for every relation**
  - generate skipped on rerun, 0 assignments inserted, event reload skipped, no dbt full refresh
  - no duplicates (count = distinct key) in users, workspaces, events, subscriptions, NPS, evaluations, assignments, `stg_events` and `int_sessions`
- **Main warehouse:** the full pipeline ran twice locally and twice in Airflow, both times with identical fingerprints (§4).

## 9. Configuration

- **Secrets:** only in `.env` (git-ignored, verified); `.env.example` holds placeholders only. No secret value appears in any tracked file or log (§ Bottom line).
- **New settings:** `POSTGRES_HOST=127.0.0.1`, `AIRFLOW_PORT=8081`, `AIRFLOW_DB=airflow`.
- **Reproducible dependencies:** `requirements/constraints-py311.txt` pins all 158 packages (incl. transitive) validated on 3.11.9, and is used by `make setup` and the Airflow image.
  - New pins with reasons: `cffi<2` / `cryptography<46` (GE vs dbt) and `protobuf<5` (dbt on Linux).
  - `requirements/pipeline.txt` = app + dbt + quality; `spectacles` moved to optional `lookml.txt`.
- **Python:** 3.11 remains the supported version (`.python-version`, `requires-python`).
- **`.dockerignore`** keeps `.venv`, `data/` and `.env` out of image builds.

## 10. Benchmarks

**Measured:**

| Stage | 10K users (782K events) | 100K users (8.25M events) | 500K users (40.5M events) |
|---|---|---|---|
| Generation | 3 s, 671 MB peak | 49 s, 707 MB | 168 s, 1,480 MB |
| Ingestion (full, COPY) | 14.2 s, 613 MB | **not measured**: run stopped by host memory pressure mid-load | not run (disk) |
| Bronze validation | 18.3 s | — | — |
| dbt build, full refresh (in pipeline) | 70.2 s | — | — |
| Gold + analytics validation | 11.0 + 6.8 s | — | — |
| Analytics | 2.4 s | — | — |
| **Pipeline total** (fresh database) | **125 s** steps / 133 s wall | — | — |
| Pipeline rerun (incremental, data unchanged) | 67–74 s wall | — | — |
| PostgreSQL container peak memory | 850 MB | — | — |
| Warehouse size | 549 MB | — | ~28 GB projected |

**dbt incremental vs full refresh** (10K users; one new day after a full build; same data both ways):

| | Runs | Median |
|---|---|---|
| Incremental (`dbt build`) | 26.8, 36.2, 19.5 s | **26.8 s** |
| Full refresh (`dbt build --full-refresh`) | 39.6, 33.5, 47.4 s | **39.6 s** |
| `verify-incremental` (single run) | 15.0 s vs 60.2 s | — |
| Airflow rerun | 23.8 s vs 84.5 s | — |

**What these numbers do and don't show:**
- **At 10K, incremental is ~1.5× faster (median), and timings vary a lot** (±30%) because the Docker VM is shared with other projects' containers. Fixed costs dominate at this size: dbt startup, 57 tests and the full-table gold models. The incremental models themselves process ~22K events instead of 782K.
- **The 100K benchmark** (`scripts/benchmark.py --users 100000`) was **killed by Claude Code's host-memory protection** while ingesting (7.7 GB host; the VM, Postgres and other projects' containers were all resident). Generation completed. The transaction rolled back cleanly, and I did not restart it.
- **500K cannot run end to end here.** The 10K warehouse is 549 MB, so 40.5M events project to ~28 GB against 6–17 GB of free disk. The generator handles 500K comfortably.
- **Not shown:** the incremental-vs-full gap at 100K/500K. I expect it to widen because the incremental window is fixed in size, but **I did not measure it**.

## 11. End-to-end validation (final)

| Command | Result |
|---|---|
| `python -m pipeline run --users 10000` (×2, `final-run-1`/`-2`) | rc 0 both times; 74 s / 67 s; **fingerprints identical** (26 relations) |
| `dbt build` | PASS=74 WARN=0 ERROR=0 |
| `ruff check .` | All checks passed |
| `pytest` | 96 passed, 2 skipped (container-only), 222 s |
| `pytest tests/test_dag.py` (Airflow container) | 3 passed |
| `pytest tests/test_spark_jobs.py` (Spark container) | 4 passed |
| Airflow `manual_run_1`, `manual_run_2` | all tasks success; fingerprints identical |

**Experiment results** (`analytics.experiment_results`, 10K users). The data changed from Phase 2 because the variant hash is now salted and independent.

| Experiment | Control → treatment (14d activation) | Lift | p | SRM p | Guardrails | Decision |
|---|---|---|---|---|---|---|
| `exp_onboarding_v2` | 18.67% → 25.38% (n = 4,966 / 5,034) | +35.9% (CI +5.1 to +8.4 pp) | < 1e-6 | 0.50 | session +3.6% (p = 0.009), revenue +9.0% (p = 0.001), both **increases** | SHIP |
| `exp_ai_summary_v1` (A/A) | 24.68% → 28.07% (n = 1,947 / 2,011) | +13.7% | **0.019** | 0.31 | session +0.7%, revenue +4.4%, n.s. | SHIP: a **false positive** |

**The A/A "SHIP" is a genuine 1-in-20 chance outcome, not a bug.** I checked:
- **Independence:** the two experiments' arms are independent (balanced crosstab).
- **Calibration:** re-randomizing the same A/A population with 400 salts gives 4.5% significant at α = 0.05 and 0.25% at α = 0.01; a test asserts the 1–10% range.
- **No seed or salt changes:** I didn't change either to hide it.
- **Why it ships:** the decision rule ships any p < 0.05 lift, so roughly 1 in 20 A/A tests will ship.

**Health tiers** (snapshot 2025-12-31): Critical 299 · At Risk 283 · Healthy 304 · Champion 114.

## 12. Files

**Added:**
- Docs: `PHASE_3_PLAN.md`, `PHASE_3_REPORT.md`, `docs/metric-definitions.md`
- Packages: `pipeline/` (`__init__`, `__main__`, `config`, `log`, `steps`, `state`, `fingerprint`, `resources`), `quality/` (`__init__`, `expectations`, `validate`)
- Python: `experimentation/experiments.py`, `scripts/benchmark.py`, `spark_jobs/__init__.py`
- dbt: `dbt_project/macros/incremental_window.sql`
- Orchestration: `dags/connecthub_pipeline.py`, `docker/airflow/Dockerfile`, `docker/airflow/init.sh`, `docker/spark/Dockerfile`, `.dockerignore`
- Requirements: `requirements/pipeline.txt`, `lookml.txt`, `constraints-py311.txt`
- Tests: `test_cli.py`, `test_dag.py`, `test_e2e_pipeline.py`, `test_pipeline_log.py`, `test_spark_jobs.py`

**Modified:**
- Experimentation and analytics: `experimentation/assignment.py` (rewrite), `experimentation/evaluate.py` (CLI, persistence, ASCII decisions), `analytics/cohort_engine.py`, `analytics/health_scoring.py` (CLIs, persistence)
- Scripts: `scripts/ingest_events.py` (partitions, shared config, no assignments), `scripts/generate_synthetic_data.py` (registry-driven treatment)
- dbt: `stg_events.sql`, `int_sessions.sql`, `int_feature_usage.sql`, `dbt_project.yml`, `tests/test_mrr_matches_active_seats.sql` (complete months only)
- Spark: both jobs
- Config: `docker-compose.yml`, `.env.example`, `.gitignore`, `Makefile`, `pyproject.toml`, `requirements.txt`, `requirements/dbt.txt`, `requirements/quality.txt`
- Docs: `README.md`, `docs/architecture.md` (rewritten to match reality)
- Tests: `test_assignment.py`, `test_evaluate.py`, `test_warehouse_parity.py`

**Removed:** `dags/product_analytics_daily.py`, `great_expectations/` (placeholder suite and checkpoint).

## 13. Remaining limitations

1. **Benchmarks above 10K are incomplete.**
   - 100K was stopped by host memory pressure during ingestion; 500K doesn't fit on this disk.
   - Incremental gains at scale are not measured.
   - Timings at 10K vary ±30% because of the shared Docker VM.
2. **Late data:** incremental models assume no late data older than `lookback_days` (3); anything later needs `--full-refresh`. A full reload of *different* data triggers it automatically; edits to old partitions do not.
3. **Experiment metrics are anchored at signup.** That suits the onboarding experiment; for `active_users` experiments like the A/A, the measured window can predate assignment.
4. **The decision rule has no multiple-testing or peeking correction.** The A/A false positive shows the expected 5% rate.
5. **Airflow:** single-node LocalExecutor; the image is large (3.39 GB) because of the separate pipeline venv. The DAG's daily schedule replays the synthetic year idempotently rather than loading new real data.
6. **Ingestion memory:** ingestion holds a 500K-row batch in pandas; the batch size could be lowered for tighter hosts.
7. **Still mocked or illustrative:** `index.html`, the AI analyst, the Hex narrative and LookML.
8. **The nested project folder remains.** `phase-2` and `phase-3` are not merged into `main`.
