# Phase 1 Report — Foundation Repair

**Date:** 2026-10-03
**Source of truth:** `PROJECT_AUDIT.md`
**Scope:** Make the existing foundation reliable and executable. No new features, no frontend changes, no Kafka/Iceberg/Spark/Airflow/FastAPI/AI analyst work.

## Bottom line

| Check | Result |
|---|---|
| Python 3.11 available | **No.** Only 3.12.10 and 3.13.0 are installed. All validation below ran on **Python 3.12**. |
| pytest | **28 passed, 0 failed, 0 skipped**: the 25 original tests plus 3 new ingestion tests, one of which runs against a live PostgreSQL |
| ruff | All checks passed |
| dependency install + `pip check` | Installs cleanly on 3.12; `pip check` finds no broken requirements. App/dev pins resolve for cp311 wheels (dry run). |
| PostgreSQL ingestion | Loads all 5 tables with `COPY`; reload replaces rather than duplicates; column types verified in `information_schema` |
| `dbt debug` / `parse` / `compile` | All pass |
| `dbt run` | **13/13 models** built into `staging`, `intermediate`, `gold`, `semantic` |
| `dbt test` | **4/4** passed |
| `docker compose config` | Valid; fails fast with a clear message when `.env` is missing |
| Every Python DB path | Runs against the built warehouse (6 functions) |

> **The dbt results are from Python 3.12 with a workaround.** dbt-core 1.7.4 imports `distutils`, which Python 3.12 removed. To validate the dbt project, `setuptools` (which supplies `distutils`) was installed into the temporary validation virtualenv only. It was **not** added to the project's requirements: on the target Python 3.11, dbt runs without it. Re-run the commands below on 3.11 once it's installed (see "Manual action").

## Update 2026-10-04: validated on Python 3.11

Manual action 1 is done. Python 3.11.9 is installed, and the project `.venv` was created with it from `requirements.txt` (`pip check`: no broken requirements). The full pipeline was then re-run on 3.11 against PostgreSQL 15 from `docker compose`, **with no `setuptools` workaround**:

| Check | Result on 3.11.9 |
|---|---|
| `pytest` | **28 passed, 0 skipped** (the integration test ran against live Postgres) |
| `ruff check .` | All checks passed |
| Generator (`--users 2000 --workspaces 200 --evaluations 2000`) | 152,108 events |
| `python -m experimentation.assignment` | 2,000 assignments (1,009 / 991) |
| `ingest_events.py --load-postgres` | 5 tables loaded |
| `dbt debug` | All checks passed |
| `dbt build` | **PASS=17 WARN=0 ERROR=0** (13 models, 4 tests); row counts match the 3.12 run |

- The project is now a git repository. The baseline commit `bb45e69` holds this Phase 1 state.
- **Observation:** the variant split differs from the 3.12 run (988 / 1,012). Assignment is deterministic per user, so the generator must produce different user IDs on each run. This is covered by Phase 2 item 1 (deterministic IDs).

Blockers 1 and 11 (the git half) below are resolved. The folder is still nested one level deep.

---

## 1. Problems fixed

### Python environment
- Standardized on **Python 3.11**:
  - `.python-version` contains `3.11`.
  - `pyproject.toml` sets `requires-python = ">=3.11,<3.12"`, with the reason documented.
  - The README documents the requirement.
- `pyproject.toml` configures pytest (`pythonpath = [".", "scripts"]`, `testpaths`, an `integration` marker), so plain `pytest` (and `make test`) now works. Previously it failed with `ModuleNotFoundError`.
- Ruff config added (`select = ["F", "E9"]`, target py311).

### Dependencies
- The one flat `requirements.txt` is now split by runtime:
  - `requirements.txt` → `requirements/app.txt` + `requirements/dbt.txt` + `requirements/dev.txt` (the dev environment)
  - `requirements/spark.txt` (optional; needs Java 17)
  - `requirements/airflow.txt` (container or separate venv only, installed with the official constraints)
  - `requirements/quality.txt` (Great Expectations, spectacles; not wired yet)
- **Replaced `dbt-spark` with `dbt-postgres==1.7.4`** (the profile is Postgres).
- **Added `SQLAlchemy==2.0.25`.** It was imported but missing. pandas 2.2's `read_sql`/`to_sql` requires SQLAlchemy ≥ 2.0.
- **Moved Airflow out of the dev environment.** Airflow 2.8 pins SQLAlchemy < 2.0, which conflicts with pandas 2.2. This is documented in `requirements/airflow.txt`.
- Removed the unused `boto3` and `kafka-python`. No code imports them, and Kafka is out of scope.
- Added `ruff==0.6.9` to dev.
- **No existing pinned version was changed.** pandas 2.2.0, numpy 1.26.3, scipy 1.12.0, dbt-core 1.7.4, pytest 8.0.0, etc. are all kept.

### Tests (7 failures → 0)
- Root cause: `stat_tests.py` returned `numpy.bool_`/`numpy.float64`, so `assert x is True` failed.
- Fixed **in the source**: `z_test_proportions`, `t_test_continuous` and `srm_check` now return native `bool`/`float`. `bayesian_ab_test` was changed the same way.
- No test was deleted and no assertion was changed. The only test-file edits removed unused imports (`import pytest` / `import numpy`) flagged by ruff.
- Side effect: evaluation results are now JSON-serializable (previously `TypeError: Object of type bool_ is not JSON serializable`).

### PostgreSQL ingestion (`scripts/ingest_events.py`)
- **SQLAlchemy 2.x compatible.** Uses `text()` and `engine.begin()` transactions instead of raw-string `conn.execute()` and `conn.commit()`, which previously raised `ObjectNotExecutableError`.
- **Explicit DDL with correct types.** For example: `signup_date`, `first_active_date`, `event_date`, `created_date` and `call_date` are `DATE`; `timestamp_utc` is `TIMESTAMP`; `assigned_at` is `TIMESTAMPTZ`. Booleans, `INTEGER` and `NUMERIC(3,1)` are typed explicitly; primary keys are set on dimension tables.
- **Bulk load via PostgreSQL `COPY`** (chunked), with `TRUNCATE` first, so reloading is idempotent.
- **`experiments.experiment_assignments` is now loaded** from `data/experiment_assignments.parquet`. Previously nothing wrote it, so `fct_experiment_assignments` could not build. A missing file leaves an empty table rather than a missing one.
- The connection comes from `POSTGRES_*` env vars (or `--conn`), and there is no hard-coded password. A `--data-dir` flag was added.
- The script now creates only the schemas it owns (`bronze`, `experiments`); dbt owns the others. The unused `silver` schema is no longer created.
- The existing `--date` parquet-filter mode (used by the DAG) is unchanged.

### dbt
- **Semantic YAML fixed.** The invalid metric spec was replaced with valid dbt 1.7 `semantic_models` and metrics:
  - `activation_rate_14d`, `weekly_retention_rate` and `ai_feature_adoption`, plus their simple component metrics.
  - The required `metricflow_time_spine` model was added.
  - `revenue_per_workspace` was **removed** because no revenue data exists. This is documented in the YAML and the semantic layer guide.
- **Schema naming.** Added `macros/generate_schema_name.sql`, so models build into exactly `staging`, `intermediate`, `gold` and `semantic` (previously `public_staging`, `public_gold`, …). Verified in Postgres: the schemas are `bronze, experiments, gold, intermediate, public, semantic, staging`.
- **Profile.** `profiles.yml` reads host, port, user, password and database from env vars. The password has no default.
- **`fct_feature_adoption`:**
  - *Interval→int error.* Staging now casts dates to `DATE` (and bronze stores `DATE`), so `event_date - signup_date` is an integer.
  - *Double counting.* Rewritten so each user counts once per feature, on their first-use day. Cumulative adoption is now a true running count of distinct users, so it **cannot exceed 100%** (observed max: 70.00%).
  - A 0–90 day spine gives each feature a complete curve.
  - New dbt test `test_feature_adoption_bounds` checks the 0–100 range and that the curve never decreases.
  - Note: the `users_adopted` column now means *users first adopting on that day*.
- **New model `gold.metrics_product_health`**, referenced by `health_scoring.py`, `evaluate.py` and LookML but never built before. It's workspace grain over a trailing 30 days, with only the columns derivable from existing data: active users / seats, features adopted, AI usage flag, average session minutes, support tickets. NPS, expansion revenue and AI-automated call share are intentionally absent because no source data exists.
- `fct_retention_cohorts`: week arithmetic now uses `DATE` values (integer days ÷ 7) instead of timezone-dependent intervals.
- Staging models cast `event_date`, `signup_date`, `first_active_date` and `created_date` to `DATE`.

### Schema-reference consistency
**Convention:** `bronze` + `experiments` (ingestion) → `staging` → `intermediate` → `gold` (+ `semantic`) (dbt). No `public_*` or `silver.*` references remain in Python, SQL, LookML, Hex or config. Spark's Iceberg namespace (`lakehouse.silver.*`) is untouched because Spark is out of scope.

| Reference (before) | Problem | Now |
|---|---|---|
| `activation_funnel.py`: `gold.fct_daily_active_users.signup_date/user_id`, `silver.events_sessionized` | Columns/table don't exist | `intermediate.int_activation_funnel` |
| `health_scoring.py`: `gold.metrics_product_health WHERE snapshot_date = CURRENT_DATE` | Model missing; date never matches 2025 data | New model; latest `snapshot_date`; only existing columns |
| `evaluate.py`: `metrics_product_health` joined on `user_id` | Workspace-grain table, nonexistent columns | `fct_experiment_assignments` + `int_activation_funnel` + `int_sessions`. The revenue guardrail returns `None` when there is no revenue data instead of computing on NaN. |
| `assignment.py`: `user_id` from `gold.fct_daily_active_users` | Column doesn't exist | `staging.stg_events` |
| `experiments.experiment_assignments` | Never written | Loaded by ingestion |
| Hex `cell_1`: `COUNT(DISTINCT user_id)` on DAU table | No `user_id` | Selects `dau, wau_7d, mau_28d` |
| LookML `product_health` view and explore joins | Undefined fields, nonexistent columns | Fields map to real `metrics_product_health` columns; joins without shared keys removed (one explore per gold table) |
| DAG `dbt run --select staging.*` | Non-idiomatic selector | `--select path:models/staging` (etc.) |

### Configuration and secrets
- Added `.env.example` (placeholders only) and ignored `.env` in `.gitignore`.
- `docker-compose.yml`:
  - All credentials come from `.env`; `POSTGRES_PASSWORD` and `AIRFLOW_ADMIN_PASSWORD` are required and fail fast if unset.
  - Postgres and Airflow ports are bound to `127.0.0.1`.
  - Fernet and webserver secret keys are passed through.
  - The obsolete `version:` key is removed.
  - Duplicated Airflow env is factored into a YAML anchor.
- `profiles.yml` and `ingest_events.py` no longer contain passwords.
- A local `.env` was created with randomly generated passwords and keys so the stack could be validated. It is git-ignored and was never printed.

### Small supporting fixes
- **Generator:** added `--users`, `--workspaces` and `--evaluations` flags. The defaults are unchanged; the full 500K-user default needs tens of GB of RAM. The `→` in print output was replaced with `->` because it crashed on the Windows cp1252 console (`UnicodeEncodeError`, reproduced during validation).
- **Makefile:**
  - `docker compose` replaces `docker-compose`.
  - New `load-postgres`, `dbt-build` and `lint` targets; `.PHONY` completed.
  - `make generate` now defaults to a 2,000-user run; override with `USERS=…`.
  - The admin/admin message was removed.
- **Lint fixes:** removed unused imports/variables (`PythonOperator`, `datetime`, `users` in health scoring) and a placeholder-less f-string in notebook 01. Added `.copy()` to silence `SettingWithCopyWarning` in health scoring.
- **Docs:** README (setup, Python version, configuration, schema table, dependency layout); `docs/semantic_layer_guide.md` (valid spec); `docs/architecture.md` (actual layer names).

---

## 2. Files changed

**Added (14):**
- `.env.example`, `.python-version`, `pyproject.toml`
- `requirements/app.txt`, `dbt.txt`, `dev.txt`, `spark.txt`, `airflow.txt`, `quality.txt`
- `dbt_project/macros/generate_schema_name.sql`
- `dbt_project/models/gold/metrics_product_health.sql`
- `dbt_project/models/semantic/metricflow_time_spine.sql`
- `dbt_project/tests/test_feature_adoption_bounds.sql`
- `tests/test_ingest_postgres.py`

Plus this report and a local, git-ignored `.env`.

**Modified (31):**
- Config: `.gitignore`, `Makefile`, `README.md`, `requirements.txt`, `docker-compose.yml`
- Analytics: `analytics/activation_funnel.py`, `analytics/health_scoring.py`
- `dags/product_analytics_daily.py`
- dbt: `dbt_project/profiles.yml`; models `gold/fct_feature_adoption.sql`, `gold/fct_retention_cohorts.sql`, `semantic/metrics_product_health.yml`, `staging/stg_events.sql`, `staging/stg_users.sql`, `staging/stg_workspaces.sql`
- Docs: `docs/architecture.md`, `docs/semantic_layer_guide.md`
- Experimentation: `experimentation/assignment.py`, `bayesian_ab.py`, `evaluate.py`, `stat_tests.py`
- Hex / LookML: `hex_notebooks/product_analytics.hex.yaml`, `lookml/models/connecthub.model.lkml`, `lookml/views/product_health.view.lkml`
- `notebooks/01_eda_exploration.ipynb`
- Scripts: `scripts/generate_synthetic_data.py`, `scripts/ingest_events.py`
- Tests (unused imports only): `tests/test_assignment.py`, `test_cohort_engine.py`, `test_power_analysis.py`, `test_stat_tests.py`

**Not touched:** `index.html`, `spark_jobs/*`, `great_expectations/*`, the other LookML views, Hex `experiment_writeup`, notebooks 02–04, the other dbt models, `PROJECT_AUDIT.md`.

---

## 3. Commands actually executed

These ran on Windows 11 in Git Bash, inside a validation venv `%TEMP%\connecthub-phase1\venv312` (Python 3.12.10). The venv and generated data files lived outside the repo; dbt `target/` and `logs/` were redirected to `%TEMP%`.

```text
py -0p ; py -3.11 --version            -> 3.13, 3.12 only; "No suitable Python runtime found"
pip install --dry-run --python-version 3.11 --only-binary=:all: (app+dev pins)  -> resolves (exit 0)
  (dbt could not be dry-run this way: its dependency logbook 1.5 is source-only)
py -3.12 -m venv ... ; pip install -r requirements.txt ; pip check  -> "No broken requirements found."
pytest                                  -> 25 passed (before ingestion tests were added)
ruff check .                            -> All checks passed!
docker compose config --quiet           -> exit 0
docker compose --env-file /dev/null config -> error: "required variable POSTGRES_PASSWORD is missing a value" (intended)
docker compose up -d postgres           -> healthy, 127.0.0.1:5432
pytest                                  -> 28 passed (integration test hit live Postgres)
python scripts/generate_synthetic_data.py --users 2000 --workspaces 200 --evaluations 2000
                                        -> first run: UnicodeEncodeError (fixed), then 152,108 events
python -m experimentation.assignment    -> 2,000 assignments (988 / 1,012)
python scripts/ingest_events.py --load-postgres (x2)  -> 5 tables loaded; second run replaced, not duplicated
psql: row counts + information_schema types  -> 152108 / 2000 / 2000; DATE/BOOLEAN/INTEGER/NUMERIC/TIMESTAMPTZ as specified
dbt --version (no shim)                 -> ModuleNotFoundError: No module named 'distutils'  (expected on 3.12)
pip install setuptools  (validation venv only)
dbt debug    -> All checks passed! (connection ok)
dbt parse    -> exit 0
dbt compile  -> 13 models, 4 tests, 5 sources, 9 metrics, 3 semantic models; exit 0
dbt run      -> PASS=13 WARN=0 ERROR=0
dbt test     -> PASS=4  WARN=0 ERROR=0
python (all 6 DB-backed functions against the warehouse) -> all OK
```

## 4. Test results
- `pytest`: **28 passed** (25 original + 3 new) in about 3 s. None were skipped.
- The new `test_copy_into_postgres_with_correct_types` writes to a throwaway schema that is dropped afterwards; it never touches `bronze`. It skips cleanly when `POSTGRES_PASSWORD` is unset or Postgres is unreachable.
- Weak spot carried over: `tests/test_cohort_engine.py` still contains 3 tautological tests that never import the module. They were kept, since you asked not to delete tests. Replace them with real tests in Phase 2.

## 5. dbt results
- `dbt run`: 13/13 models built.
  - `semantic.metricflow_time_spine` (1,461 rows)
  - `staging`: `stg_events`, `stg_users`, `stg_workspaces` (views)
  - `intermediate`: `int_feature_usage` (91,787), `int_sessions` (120,143), `int_activation_funnel` (2,000)
  - `gold`: `fct_agent_evaluations` (2,000), `fct_experiment_assignments` (2,000), `fct_retention_cohorts` (602), `fct_feature_adoption` (637), `fct_daily_active_users` (365), `metrics_product_health` (200)
- `dbt test`: 4/4 passed: `test_stg_events_not_null`, `test_retention_rate_bounds`, `test_experiment_no_duplicates`, `test_feature_adoption_bounds` (new).
- Performance warning: `fct_daily_active_users` took about 14 s on just 152K events (correlated subqueries). It will not scale to the full dataset; see Phase 2.

## 6. Docker results
- Docker Desktop was installed but stopped; I started it to test against a real Postgres.
- `docker compose config`: valid, with no obsolete-`version` warning.
- Postgres 15 started from compose with credentials from `.env`; healthy, published on `127.0.0.1:5432` only. **It is still running.** Stop it with `docker compose down` (data persists in the `postgres_data` volume).
- Airflow containers were **not** started; Airflow is out of scope this phase.

---

## 7. Remaining blockers

| # | Blocker | Impact |
|---|---|---|
| 1 | ~~**Python 3.11 is not installed.**~~ **Resolved 2026-10-04:** validated end to end on 3.11.9 (see the update at the top). | — |
| 2 | Airflow image still lacks dbt, PySpark, Java and the project packages; DAG tasks use relative paths; no init service | The DAG cannot run (out of scope; Phase 7 in the audit) |
| 3 | Spark untested (no Java). Its outputs still go to parquet or Iceberg, not Postgres; `int_sessions` uses the generator's per-day session ids | Spark-derived sessions are not used by the warehouse |
| 4 | Still mocked: `index.html` numbers and AI analyst; `evaluate_from_parquet` (random conversions, now labeled in code); parquet-path health inputs (random NPS, revenue, AI %) | Dashboard and parquet-based experiment results are not real |
| 5 | Synthetic data has no behavioral signal, and the 500K default is infeasible on a laptop. On 2K users, health tiers land at 182 Critical / 18 At Risk / 0 Healthy / 0 Champion, and the experiment shows no effect. | Analytics outputs are mechanically correct but uninformative |
| 6 | No revenue or NPS data anywhere, so `revenue_per_workspace` and the revenue guardrail can't be computed | Experiment guardrail is reported as `None` |
| 7 | `fct_daily_active_users` is quadratic | Will not finish at full scale |
| 8 | Great Expectations has no data context; the DAG's GE task cannot run | Data-quality step is non-functional |
| 9 | LookML not validated (no Looker instance); `percent_*` formats on 0–100 values in other views remain | Cosmetic and validation risk |
| 10 | Notebooks still assume CWD = repo root | Running from `notebooks/` fails |
| 11 | ~~The project is not a git repository~~ (resolved: baseline commit `bb45e69`). The folder is still nested one level deep. | Cosmetic |

## 8. Manual action required
1. **Install Python 3.11**, e.g. `winget install Python.Python.3.11` or python.org. Then, from the project root:
   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   Re-run `pytest`, `ruff check .` and, in `dbt_project`, `dbt debug`, `parse`, `compile`, `run` and `test`. No `setuptools` shim should be needed.
2. **Review `.env`.** It was generated with random passwords. The Postgres volume is initialized with that password, so if you change `POSTGRES_PASSWORD` later, run `docker compose down -v` to recreate the volume.
3. **Export `.env` before running dbt or the scripts.** dbt does not read `.env` itself; see the README "Configuration" section for bash and PowerShell commands.
4. Recommended: `git init` and commit a baseline (and optionally flatten the nested folder) before Phase 2.
5. Stop Postgres when you're done: `docker compose down`.

## 9. Recommended Phase 2: realistic data and real metrics
1. **Rewrite the synthetic data generator.** Make it vectorized and memory-bounded, and model behavior:
   - an ordered activation funnel and retention decay
   - plan-based revenue/MRR and NPS responses
   - support tickets tied to health
   - a planted, known experiment effect keyed off `assign_variant`
   - deterministic IDs
2. **Extend bronze and dbt** with revenue and NPS sources, and add those columns to `metrics_product_health`. Restore `revenue_per_workspace` as a valid metric.
3. **Replace the simulations.** `evaluate_from_parquet` should compute real per-user metrics, the revenue and session-duration guardrails should use real data, and parquet-path health scoring should drop its random inputs. Recalibrate the health-score tier thresholds.
4. **Rewrite `fct_daily_active_users`** with a date spine and range joins so it scales.
5. **Replace the tautological `test_cohort_engine.py` tests** with real ones against a small fixture dataset. Add dbt generic tests (`schema.yml`: unique/not_null/relationships).
