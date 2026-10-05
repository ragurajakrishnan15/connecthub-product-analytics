# ConnectHub Product Analytics — Project Audit

**Audit date:** 2026-10-03
**Scope:** Every file in the repository (61 files) was read in full. No project files were modified. All checks ran with caches and outputs redirected to a temporary scratch directory.
**Audited path:** `connecthub-product-analytics/connecthub-product-analytics/` (the repo is nested one folder deep and is **not a git repository**)

Severity legend: **P0** = blocks the project from running · **P1** = wrong results or a broken feature · **P2** = quality, maintainability, or hygiene

---

## 0. Executive summary

The repository is a **portfolio-style scaffold**, not a working pipeline. The individual pieces exist (generator, Spark jobs, dbt models, DAG, experimentation library, dashboard), but **they are not connected end-to-end and most of the integration paths cannot run**:

- `pip install -r requirements.txt` **cannot succeed on this machine**. It pins Airflow 2.8.1, which requires Python < 3.12, and only Python 3.12 and 3.13 are installed. This was verified with `pip --dry-run`.
- **dbt cannot parse the project.** The `semantic/` metrics YAML uses a spec that does not exist (verified with `dbt parse`). Beyond that, the adapter is wrong: `dbt-spark` is installed while the profile uses Postgres. dbt 1.7.4 also crashes on Python 3.12 (`No module named 'distutils'`).
- **Every downstream consumer queries schemas that dbt never creates.** Python, LookML and Hex all query `gold.*`, but dbt writes to `public_gold.*` (verified with `dbt ls`). Several consumers also query tables or columns that no model produces (`gold.metrics_product_health`, `silver.events_sessionized`, `fct_daily_active_users.user_id`).
- **Loading data into Postgres fails.** `ingest_events.py` passes raw SQL strings to SQLAlchemy 2.x (verified: `ObjectNotExecutableError`).
- **The dashboard (`index.html`) is 100% hard-coded** and has no backend. The "AI Analyst" only works inside the Claude artifact runtime (`claude.use('sample')`). Everywhere else, including the advertised GitHub Pages site, it falls back to keyword-matched canned answers.
- **The experiment evaluation is simulated.** `evaluate_from_parquet` never looks at events. It draws random conversions with a hard-coded 0.32 → 0.34 "lift" and random revenue.
- **7 of 25 unit tests fail** (`numpy.bool_` vs `is True`/`is False`). The suite only collects under `python -m pytest`; plain `pytest` (which the Makefile uses) fails with `ModuleNotFoundError`.
- The **pure-Python statistics code** (power analysis, z-test, Welch t-test, SRM, Bayesian A/B, hashing assignment) is correct and reusable. The **pandas "from parquet" analytics paths** run on small data.

---

## 1. Environment observed during audit

| Tool | Status on this machine |
|---|---|
| Python | 3.13.0 (default), 3.12.10. **No 3.11** (the only version that satisfies Airflow 2.8.1). `python` on PATH is the Microsoft Store stub; use `py -3.12`. |
| Java | **Not installed**, so Spark cannot run |
| spark-submit / dbt / make | Not installed / not on PATH |
| Docker | CLI 29.7.2 present; **daemon not running** (Docker Desktop stopped) |
| Node | Present (not used by the project) |
| git | Present, but the project folder is not a git repo |

---

## 2. Current folder structure

```
connecthub-product-analytics/                 <- outer wrapper folder (only contains the inner folder)
└── connecthub-product-analytics/             <- actual project root
    ├── index.html                            Static dashboard + "AI analyst" (single file, ~1,000 lines)
    ├── Makefile, requirements.txt, docker-compose.yml, README.md, .gitignore
    ├── scripts/        generate_synthetic_data.py, ingest_events.py
    ├── spark_jobs/     sessionize_events.py, feature_extraction.py
    ├── dbt_project/    dbt_project.yml, profiles.yml, models/{staging,intermediate,gold,semantic}, tests/ (3 singular tests)
    ├── dags/           product_analytics_daily.py
    ├── great_expectations/  checkpoints/events_checkpoint.py (a Python dict), expectations/events_suite.json
    ├── analytics/      activation_funnel, cohort_engine, feature_adoption, health_scoring
    ├── experimentation/ assignment, bayesian_ab, evaluate, power_analysis, stat_tests
    ├── lookml/         1 model + 4 views
    ├── hex_notebooks/  2 YAML "notebook configs"
    ├── notebooks/      4 Jupyter notebooks (no saved outputs)
    ├── tests/          4 pytest files (25 tests)
    ├── docs/           architecture.md, experiment_playbook.md, semantic_layer_guide.md
    └── data/           .gitkeep only (no data generated)
```

Structural issues:
- The double nesting (`connecthub-product-analytics/connecthub-product-analytics`) should be flattened.
- There is no `pyproject.toml`, `setup.cfg`, `conftest.py` or `pytest.ini`, so the packages (`analytics`, `experimentation`) are only importable when the CWD is on `sys.path`.
- There is no `.env` / `.env.example`, no CI config, and no lint config.
- There is no backend/API directory. The "frontend" is one static HTML file.

---

## 3. Area-by-area analysis

### 3.1 Frontend architecture (`index.html`)
- A single static page using Chart.js 4.4.1 (cdnjs) and Google Fonts. It has no build step, no framework and **no data fetching**.
- **Every number is a JS literal**: the KPIs, DAU series, adoption curves, revenue, heatmap, funnel, experiment results, health tiers, and the at-risk table with invented names ("Acme Corp", "TechFlow Inc", …).
- **The displayed data contradicts the pipeline.**
  - Dates: the header says "Oct 2025 — Sep 2026", but the generator produces calendar year 2025.
  - Workspaces: the analyst context says "22,000+ businesses", but the generator creates 50,000 workspaces.
  - Experiment size: the experiment shows n = 250,000, but `assignment.py` assigns all 500,000 users.
  - Revenue and NPS: MRR, NPS and revenue-by-plan are shown, but **no revenue or NPS data exists anywhere** in the generator or dbt.
  - Health tiers: the "Champion 16.8% / Healthy 44.3%" tiers are unreachable by the actual scoring code (§3.10).
- Bug: the health distribution chart sets `backgroundColor` to a function that returns an array. Scriptable options must return a single value, so the bars do not get the intended colors. The line `document.getElementById('chart-health-dist').__chartjs = true; // Fix bar colors` is a no-op.
- A "Live" pill and a pulsing green dot imply live data. There is none.
- Positives: the responsive layout is reasonable, and user and AI text go through `textContent`, which is safe.

### 3.2 Backend architecture
- **None exists.** There is no API server, no service layer, and nothing that serves gold tables to the dashboard. The "serving layer" in the docs (Hex, Looker) consists of configuration files with no deployment.

### 3.3 Database configuration
- One Postgres 15 container (`connecthub_analytics` DB). The schemas `bronze`, `silver`, `intermediate`, `gold`, `semantic` and `experiments` are created ad hoc by `ingest_events.py`; that step currently fails (§3.13).
- **The Airflow metadata DB is the same database** as the analytics data (`AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` points at `connecthub_analytics`), so the Airflow tables are mixed into `public`.
- `dbt_project/profiles.yml` uses `host: localhost`. That works from the host but **not from inside the Airflow containers**, which would need `host: postgres`.
- dbt's default `generate_schema_name` combines `schema: public` with `+schema: gold`, which gives **`public_gold`** (verified). No consumer uses that name.
- There are no DDL or migration files. Bronze tables are created by `pandas.to_sql(if_exists='replace')`, so column types are whatever pandas infers. For example, `signup_date` becomes `TIMESTAMP`, not `DATE`, which breaks `fct_feature_adoption` (§3.15).

### 3.4 Docker configuration (`docker-compose.yml`)
`docker compose config` is valid apart from a warning that `version` is obsolete. Services: `postgres`, `airflow-webserver` and `airflow-scheduler`.

| Issue | Severity |
|---|---|
| The stock `apache/airflow:2.8.1` image has **no dbt, PySpark, Java, Great Expectations, scipy, scikit-learn or seaborn**. Every DAG task would fail with "command not found" or an ImportError. | P0 |
| BashOperator commands use relative paths (`python scripts/...`, `cd dbt_project`). BashOperator runs in a temp dir, so the files would not be found. There is no `cwd=` and no `PYTHONPATH=/opt/airflow`. | P0 |
| There is no `airflow-init` service. The scheduler starts in parallel with the webserver's `airflow db init` and can crash on an uninitialized DB. There is no `restart:` policy. | P1 |
| `airflow db init` is deprecated in 2.7+ (`airflow db migrate`). | P2 |
| No Fernet key and no webserver secret key. Default admin/admin. Postgres is exposed on host `0.0.0.0:5432` with a weak password. | P1 (security) |
| Services the docs claim but that are **missing**: Spark (master/worker), an S3/MinIO + Iceberg catalog, Kafka/Kinesis, a Great Expectations data context, a backend API, a dashboard web server, and Looker/Hex (external SaaS). | P1 |

### 3.5 dbt configuration
Verified with `dbt-core 1.7.4` + `dbt-postgres 1.7.4` in a scratch venv:

1. **P0:** `dbt parse` fails with `Compilation Error: Could not render {{ Dimension('user__days_since_signup') }} <= 14: 'Dimension' is undefined`. `models/semantic/metrics_product_health.yml` uses a made-up or legacy metric spec. Ratio metrics reference measures (`activated_users`, `total_signups`, …) and no `semantic_models` exist. The derived metric has no `metrics:` inputs.
2. **P0:** `requirements.txt` installs `dbt-spark`, but `profiles.yml` is `type: postgres`. `dbt-postgres` is not listed.
3. **P0:** dbt 1.7.4 does not start on Python 3.12 (`ModuleNotFoundError: No module named 'distutils'`). It needs Python ≤ 3.11, dbt ≥ 1.8, or `setuptools` installed as a shim.
4. **P1:** With `semantic/` removed, parsing succeeds. The models resolve to `public_staging`, `public_intermediate` and `public_gold` (no custom `generate_schema_name` macro), and every consumer expects `gold.*` / `intermediate.*`.
5. **P1:** `fct_experiment_assignments` reads `source('experiments','experiment_assignments')`. **Nothing ever writes that table**: `assignment.py` only writes parquet. `dbt run` will fail on this model and its test.
6. No `schema.yml`: there are no generic tests (unique/not_null/relationships/accepted_values), no column docs, and no source freshness. The only tests are 3 singular SQL tests.
7. The project says "Bronze → Silver → Gold", but the models are `staging / intermediate / gold`. The `silver` schema is created by ingest and never used.
8. `fct_agent_evaluations` reads the bronze source directly from the gold layer and skips staging.
9. `ORDER BY` inside table materializations is wasted work.

### 3.6 Spark / PySpark (`spark_jobs/`)
These could not be executed because Java is not installed. Static review:
- `create_spark_session()` **always** configures an Iceberg catalog on `s3://connecthub-lakehouse/`, even in `--local` mode. No Iceberg runtime jar or `--packages` is specified, and there are no S3/hadoop-aws credentials or jars. Production mode (`lakehouse.bronze.events_raw`) cannot work. Local mode will probably still run, because Spark logs and skips missing extension classes, but this is unverified.
- **The Spark outputs are orphaned.**
  - `events_sessionized.parquet` and `feature_usage.parquet` are never loaded into Postgres. dbt's `int_sessions` uses the generator's fake `session_id` (`user_id + day`), not Spark's 30-minute-gap sessions.
  - `analytics/activation_funnel.py` queries `silver.events_sessionized`, which does not exist in Postgres.
- In the DAG, `spark_sessionize` runs **production (Iceberg) mode** with `{{ ds }}`, but `spark_feature_extraction` reads the **local** parquet path (`data/events_sessionized.parquet`). The two tasks are inconsistent.
- The sessionization logic itself (lag, gap > 30 min, cumulative sum) is correct. The code is duplicated between `sessionize_events` and `sessionize_from_parquet`.
- `feature_extraction.py` partitions output by `event_date` (365 partitions × many small files at this scale). This is fine but adds file overhead.
- Installed versions: PySpark 4.2.0 on Python 3.12 vs `pyspark==3.5.1` pinned.

### 3.7 Airflow DAG (`dags/product_analytics_daily.py`)
- It parses as Python. `PythonOperator` is imported but unused, and `schedule_interval` is deprecated (`schedule`).
- **Task problems:**
  - `ingest_raw_events` only filters `data/events.parquet` to `data/ingested/events.parquet`. It does **not** load Postgres, so dbt would have no new data.
  - `run_great_expectations` fails: there is no `great_expectations.yml` / data context, the checkpoint is a Python dict rather than a registered checkpoint, and the datasource `connecthub_postgres` is not defined.
  - `dbt run --select staging.*`: the idiomatic selector is `--select staging` (or `path:models/staging`). The task also inherits all the dbt P0s.
  - `cohort_analysis` runs `python -m analytics.cohort_engine --date …`. **The module has no `__main__`**, so it exits 0 having done nothing (a silent success).
  - `experiment_evaluation` ignores `--date` and runs the **simulated** parquet evaluation.
  - The Spark tasks have no Spark in the image and use inconsistent modes (§3.6).
- Ordering: Spark runs before dbt, but dbt does not consume the Spark output.
- `slack_alert` only prints; there is no Slack integration. `context['execution_date']` is deprecated.

### 3.8 Experimentation (`experimentation/`)
| File | State |
|---|---|
| `power_analysis.py` | **Works.** The formulas are correct. |
| `stat_tests.py` | **Works mathematically.** It returns `numpy.bool_` / `numpy.float64`, which breaks `is True` tests and `json.dumps` (verified: `TypeError: Object of type bool_ is not JSON serializable`). `2*(1-cdf)` loses precision for large z; use `stats.norm.sf`. |
| `bayesian_ab.py` | **Works.** Calls `np.random.seed(42)` globally (a side effect on the caller's RNG). `risk_of_choosing_treatment` is just `1 − P(better)`, which is mislabeled. |
| `assignment.py` | `assign_variant` is deterministic and balanced. The traffic bucket and the variant come from the **same hash** (`h % 10000` and `h % n`), so they are correlated. Use a salted second hash. The DB path `assign_experiment_cohort` queries `user_id` from `gold.fct_daily_active_users`, **which has no `user_id` column**, and it never persists assignments. |
| `evaluate.py` | **Mocked.** `evaluate_from_parquet` loads events and never uses them. Conversions are `np.random.binomial(n, 0.32 / 0.34)`, and session and revenue are random. The revenue guardrail runs a t-test on randomly generated "individual values". Results change between runs. In the smoke test it returned "REVERT — Revenue guardrail failed" on both runs. The DB path joins `gold.metrics_product_health` on `user_id` with columns `activation_14d`, `session_duration_minutes` and `revenue_per_user_30d`; **no such table or columns exist**. `_evaluate` hard-codes `variant_0` / `variant_1`. The session-duration guardrail listed in the playbook is not implemented. |

### 3.9 AI analyst implementation (in `index.html`)
- `initAI()` calls `claude.use('sample')`, an API that only exists when the page runs inside the Claude artifact runtime.
- **On GitHub Pages or locally**, `claude` is undefined. A ReferenceError is caught silently, and every question goes to `getFallbackResponse()`, a **6-branch keyword `if` chain returning canned strings**.
- When the runtime is available, the model gets a hard-coded `PROJECT_CONTEXT` string of the same fabricated metrics. **It has no access to real data**: no SQL, no tool use, no retrieval.
- There is no backend proxy, no conversation history (each message is sent alone), and no guardrails.
- **It needs a rewrite:** a server-side endpoint that calls an LLM API, keeps the key out of the browser, and grounds answers in gold tables (for example, a read-only SQL tool with an allow-listed schema).

### 3.10 Analytics modules (`analytics/`)
Each module has a DB path and a parquet path.

| Module | DB path | Parquet path (smoke-tested on 2K users) |
|---|---|---|
| `cohort_engine` | SQL is correct but uses `gold.` (actual schema is `public_gold`) | Runs. Week-0 retention ≈ 45–50% (the random generator gives no signup-week activity guarantee). It uses `signup_date`; dbt uses `first_active_date`, which is inconsistent. |
| `activation_funnel` | **Broken**: selects `user_id, signup_date` from `gold.fct_daily_active_users` (neither column exists) and joins `silver.events_sessionized` (not in Postgres) | Runs. The funnel is **not sequential**: "Used AI" (551) > "Placed Call" (309) because stages are not conditioned on earlier stages. |
| `feature_adoption` | SQL is fine apart from the schema name | Runs |
| `health_scoring` | **Broken**: `gold.metrics_product_health` does not exist | Runs, but **4 of 7 inputs are `np.random`** (AI %, session duration, NPS, expansion revenue). It raises a `SettingWithCopyWarning` and leaves `users` unused. **The tier thresholds are effectively unreachable**: the positive weights sum to 0.90, so "Champion" (≥ 80) needs nearly every metric at its max at once. Smoke result: 0 Healthy, 0 Champion. |

There are **four different event→feature maps**: dbt `int_feature_usage` / Spark (13 events), `feature_adoption.py` (9), and `health_scoring.py` (5). They should share one source of truth.

### 3.11 Tests
- `py -3.12 -m pytest tests/`: **18 passed, 7 failed.** The same result on 3.13.
  - Failures: `test_stat_tests.py` — `test_no_difference`, `test_large_sample_detects_small_diff`, `test_small_sample_no_detection`, `test_significant_difference`, and the 3 SRM tests. Cause: `assert np.False_ is False`.
- Without CWD on `sys.path` (`py -P -m pytest`, equivalent to the bare `pytest` used by `make test`): **collection fails** with `ModuleNotFoundError: No module named 'experimentation'`.
- `test_cohort_engine.py` **never imports the cohort engine**. All 3 tests are tautologies on hand-made literals.
- No tests cover `analytics/*`, `evaluate.py`, `bayesian_ab.py`, the generator, the Spark jobs (no local SparkSession fixture), DAG import integrity, or dbt models in CI.

### 3.12 Documentation
- `README.md`: the "Live Dashboard" links point at `yourusername.github.io` (placeholder). It claims "50M+ events"; the projection is about 38M (§3.13). It says `make run-all` runs "the full pipeline", but that target skips Docker, ingestion, dbt and Airflow.
- `docs/architecture.md` describes Kafka, Kinesis, S3, Iceberg, Slack alerts and Looker. **None of these are implemented.**
- `docs/semantic_layer_guide.md` teaches the same invalid metric spec that breaks `dbt parse`. `spectacles sql` needs a live Looker instance.
- `docs/experiment_playbook.md` matches the code, but step 5 produces simulated results.
- `hex_notebooks/*.hex.yaml` is not a Hex import format, and the cells hard-code conclusions ("Decision: SHIP", "DAU grew 12.3%"). `cell_8` uses an undefined `conn`. `cell_1` runs `COUNT(DISTINCT user_id)` on `fct_daily_active_users`, which has no `user_id`.
- `lookml/` would fail Looker validation:
  - It references undefined fields (`${activated_users}`, `${total_signups}`, `${ai_active_workspaces}`, `${total_active_workspaces}`, `product_health.cohort_week`, `feature_adoption.workspace_id`).
  - The `health_tier` dimension references a measure.
  - `variant_distribution` uses a window function inside a measure.
  - `percent_*` formats are applied to values already on a 0–100 scale (they would show as 3840%).
  - The target table `gold.metrics_product_health` does not exist.
- `notebooks/*.ipynb` use `data/...` paths and package imports that assume CWD = repo root. Jupyter starts in `notebooks/`, which gives FileNotFoundError / ModuleNotFoundError. No saved outputs.

### 3.13 Data generation & ingestion (`scripts/`)
- `generate_synthetic_data.py` runs at small scale (verified). **At the configured scale it is impractical:**
  - Measured ~4.7 ms per user, which projects to **~40 minutes**.
  - Projected **~38M events** (not "50M+").
  - The final DataFrame alone is **~23 GB** (610 B/row), plus a Python list of 38M dicts held in memory before it. **That will likely run out of memory on a typical laptop.**
- **The data has no behavioral signal**, so the analytics cannot show the dashboard's stories:
  - Event names are uniformly random.
  - There are about 3.6 `user.signup` events per user, plus random `workspace.created` events.
  - There is no funnel ordering, no retention decay and no experiment effect.
  - There is no revenue, MRR or NPS.
  - About 38% of users sign up *before* their workspace is created.
  - Agent evaluations are independent of users, and 8.4% are both `resolved_by_ai` and `escalated_to_human`.
  - `signup_date` is clipped to the end date, which creates a spike on day 365.
  - `uuid4` IDs make runs non-reproducible despite `np.random.seed(42)`.
- `ingest_events.py --load-postgres` **fails**: `conn.execute("CREATE SCHEMA …")` with a raw string raises `ObjectNotExecutableError` on SQLAlchemy 2.x (verified). On SQLAlchemy 1.4, which Airflow 2.8 pins, `conn.commit()` does not exist and pandas 2.2's `to_sql` requires SQLAlchemy ≥ 2.0. So it is broken on both. `sqlalchemy` is not in `requirements.txt` either. Even when fixed, `to_sql` with `chunksize=50000` for ~38M rows is very slow; use `COPY`.

### 3.14 Dependencies (`requirements.txt`)
Everything is in one flat file mixing the Airflow server, dbt, Spark, GE, notebooks and tests.

| Conflict / problem | Evidence |
|---|---|
| `apache-airflow==2.8.1` requires Python < 3.12. **It is uninstallable on 3.12 and 3.13**, which are the only versions on this machine. | `pip install --dry-run`: "No matching distribution found for apache-airflow==2.8.1" |
| Airflow 2.8.1 constrains `SQLAlchemy<2.0`, while `pandas==2.2.0` `to_sql` requires SQLAlchemy ≥ 2.0 | Known constraint; it would surface on Python 3.11 |
| Airflow is installed without its official constraints file, so resolution is fragile and slow | — |
| `numpy==1.26.3`, `pandas==2.2.0`, `scipy==1.12.0` and `pyarrow==15.0.0` have no Python 3.13 wheels | — |
| `dbt-spark` is listed but the profile is Postgres; `dbt-postgres` is missing | — |
| `dbt-core==1.7.4` crashes on Python 3.12 (distutils) | Verified |
| `great-expectations==0.18.8` and `dbt-core 1.7` both pin assorted transitive dependencies (jsonschema, protobuf, …); likely conflicts with Airflow | Unverified (needs Python 3.11) |
| **Unused:** `boto3`, `kafka-python`, `spectacles` (CLI only, needs Looker), `great-expectations` (no context) | grep shows no imports |
| **Missing:** `sqlalchemy` (imported), `dbt-postgres`, `jupyter`, plus a Java/JDK requirement for Spark | — |

### 3.15 Broken SQL / schema mismatches (consolidated)
| Location | Problem | Severity |
|---|---|---|
| `models/semantic/metrics_product_health.yml` | Invalid metric spec, so dbt parse fails | P0 |
| All Python/LookML/Hex SQL | `gold.*` / `intermediate.*`, but the actual schemas are `public_gold` / `public_intermediate` | P0 |
| `gold/fct_feature_adoption.sql` | `(fu.event_date - u.signup_date)::INT`: `signup_date` is TIMESTAMP (pandas), so date − timestamp = **interval**, and Postgres cannot cast interval to integer, which is a runtime error | P0 |
| `gold/fct_feature_adoption.sql` | `cumulative_adoption_pct` = running **SUM of daily distinct users**, so a user active on 50 days counts 50 times and values can exceed 100% | P1 |
| `gold/fct_daily_active_users.sql` | Correlated `COUNT(DISTINCT)` subqueries per date over tens of millions of rows; O(days × rows), impractical | P1 |
| `gold/fct_experiment_assignments.sql` | The source table `experiments.experiment_assignments` is never populated | P0 for `dbt run` |
| `gold/fct_retention_cohorts.sql` | LEFT JOIN negated by `WHERE a.activity_week >= …`. Mixed `timestamp`/`timestamptz` from `DATE_TRUNC(date)` gives a DST risk in week bucketing if the session TZ is not UTC | P2 |
| `intermediate/int_sessions.sql` | Uses the generator's fake `session_id`, not Spark sessions | P1 |
| `analytics/activation_funnel.py` | `fct_daily_active_users.user_id/signup_date` don't exist; `silver.events_sessionized` not in Postgres | P0 (that path) |
| `analytics/health_scoring.py`, `experimentation/evaluate.py`, `lookml/product_health.view.lkml` | `gold.metrics_product_health` **is not produced by any model**, and its columns are undefined (`nps_score`, `expansion_revenue_30d`, `activation_14d`, `revenue_per_user_30d`, …) | P0 (that path) |
| `experimentation/assignment.py` (DB path), `hex product_analytics cell_1` | `fct_daily_active_users.user_id` doesn't exist | P0 (that path) |

### 3.16 Hard-coded / mock data (inventory)
- `index.html`: every KPI, chart series, heatmap cell, funnel stage, experiment stat, health tier count, at-risk row, the insight text, and the entire AI context and fallback answers.
- `experimentation/evaluate.py`: simulated conversions, session duration, revenue and guardrail samples.
- `analytics/health_scoring.py`: random AI %, session duration, NPS and expansion revenue.
- `hex_notebooks/*.yaml`: hard-coded insights and "Decision: SHIP".
- `scripts/generate_synthetic_data.py`: synthetic by design, but with no behavioral model (see §3.13).
- `dags/…: slack_alert`: prints instead of alerting.
- `README.md`: placeholder `yourusername` URLs.

### 3.17 Security
| Issue | Severity |
|---|---|
| Credentials committed in plain text: `profiles.yml` (`connecthub_dev`), `docker-compose.yml` (Postgres password, Airflow `admin/admin`), and the `ingest_events.py` default connection string | P1 |
| Postgres published on all host interfaces (`5432:5432`) | P1 |
| No Airflow Fernet key, so connections and variables would be stored unencrypted; no webserver secret key | P1 |
| CDN scripts loaded without Subresource Integrity (`chart.umd.min.js`) | P2 |
| `innerHTML` templating in the dashboard is safe today (static literals) but becomes **XSS** once workspace names come from data | P2 (future) |
| A future LLM analyst with SQL access needs a read-only DB role, a schema allow-list and prompt-injection-aware tool design; the API key must never reach the browser | Design note |
| No secrets were found beyond the dev passwords above | — |

### 3.18 Configuration problems
- `make` is not available on Windows by default. The Makefile also uses POSIX `rm -rf` and leaves `spark-features`, `run-experiments` and `run-all` out of `.PHONY`.
- `make test` uses bare `pytest`, which fails to collect (§3.11).
- `make run-all` does not include ingestion, dbt or the DAG, and `spark-session` requires a Spark install plus Java.
- The `dbt_project.yml` `semantic` config path doesn't apply to any model (warning).
- Console output uses emoji and em dashes, which can raise `UnicodeEncodeError` on legacy Windows consoles (cp1252).
- The project is not under version control.

---

## 4. What currently works
Verified on Python 3.12 unless noted.
- `experimentation/power_analysis.py`, `stat_tests.py`, `bayesian_ab.py` and `assignment.assign_variant`: the math is correct, with 18 passing unit tests.
- `python -m experimentation.power_analysis` and `python -m experimentation.bayesian_ab` (CLI demos).
- `scripts/generate_synthetic_data.py` functions at **small** scale.
- Parquet paths: `generate_retention_from_parquet`, `build_funnel_from_parquet`, `adoption_from_parquet`, `compute_health_from_parquet`, `assign_from_parquet`, `evaluate_from_parquet`. They all execute, but several produce mock or meaningless output.
- `dbt_project.yml` + `profiles.yml` are valid (`dbt debug`). The models parse once `models/semantic/` is removed.
- `docker-compose.yml` is syntactically valid.
- `index.html` renders as a static demo in any browser.
- The Spark sessionization logic is correct in principle (not executed: no Java).

## 5. What currently fails
| # | Failure | Verified? |
|---|---|---|
| 1 | `pip install -r requirements.txt` on Python 3.12/3.13 | Yes |
| 2 | 7/25 unit tests (`numpy.bool_` identity) | Yes |
| 3 | `make test` / bare `pytest`: collection ImportError | Yes |
| 4 | `dbt parse` / `run` / `test`: semantic YAML compilation error | Yes |
| 5 | dbt 1.7.4 on Python 3.12: `distutils` ImportError | Yes |
| 6 | dbt adapter missing (`dbt-postgres` not in requirements) | By inspection |
| 7 | `ingest_events.py --load-postgres`: SQLAlchemy `ObjectNotExecutableError` | Yes |
| 8 | `fct_feature_adoption`: interval→int cast error | By inspection (Postgres not running) |
| 9 | `fct_experiment_assignments`: missing source relation | By inspection |
| 10 | Every DB-backed analytics/experiment function: wrong schema and/or nonexistent tables | By inspection + `dbt ls` |
| 11 | All Airflow DAG tasks (missing tooling in image, relative paths, no GE context) | By inspection |
| 12 | Spark production mode (Iceberg/S3 not configured); Spark at all on this machine (no Java) | By inspection |
| 13 | `json.dumps(evaluate result)` without `default=str` | Yes |
| 14 | Full-scale data generation (time/memory) | Projected from measurement |
| 15 | Notebooks run from `notebooks/` (paths/imports) | By inspection |
| 16 | AI analyst outside the Claude runtime (canned fallback) | By inspection |
| 17 | LookML validation; Hex YAML import | By inspection |

## 6. What is incomplete
- There is no data path from Spark into the warehouse, and no Silver layer in Postgres.
- `gold.metrics_product_health` (the workspace health model) and a user-level experiment metrics model are both missing.
- There is no writer for `experiments.experiment_assignments`.
- No revenue, MRR, NPS or support-ticket model; the dashboard and LookML assume them.
- No backend API, and no way for the dashboard to load real data.
- No real AI analyst (grounding, tools, server-side key).
- No Great Expectations data context or datasource.
- No dbt `schema.yml` tests or docs, no source freshness, no semantic models.
- No Airflow image with project dependencies, no init service, no connections or variables.
- No Iceberg/S3/MinIO, Kafka or Spark cluster services.
- No CI, linting config, packaging (`pyproject.toml`) or `conftest.py`.
- `cohort_engine` has no CLI entrypoint, although the DAG calls one.

## 7. What is mocked
See §3.16. The most consequential mocks: **all dashboard numbers, the AI analyst, experiment evaluation results, and 4 of 7 health-score inputs.**

## 8. What needs to be rewritten (not just patched)
| Component | Why rewrite |
|---|---|
| `scripts/generate_synthetic_data.py` | A row-by-row Python loop can't reach the target scale, and the data has no behavioral model (funnel order, retention decay, experiment effect, revenue, NPS). Needs vectorized numpy, a scale parameter, chunked or partitioned parquet output, and deterministic IDs. |
| `scripts/ingest_events.py` | SQLAlchemy 2.x API, `COPY`-based bulk loading, explicit DDL with correct types (`DATE` vs `TIMESTAMP`), and loading of experiment assignments and Spark outputs. |
| `experimentation/evaluate.py` | Replace the simulation with real per-user metrics from dbt (activation flag, revenue, session duration); return native Python types. |
| `index.html` data layer + AI analyst | Fetch from a backend API instead of literals. The AI analyst needs a server-side LLM endpoint with grounded data access. |
| `models/semantic/metrics_product_health.yml` | Replace with valid `semantic_models` + metrics, or delete and implement `metrics_product_health` as a normal gold model. |
| `fct_daily_active_users.sql`, `fct_feature_adoption.sql` | Correctness (double counting) and performance issues. |
| `docker-compose.yml` | Custom Airflow image, separate metadata DB, init service, env-file secrets, optional Spark/MinIO. |
| `dags/product_analytics_daily.py` | Correct task wiring, `cwd`, a real load step, consistent Spark mode, real alerting. |
| `lookml/`, `hex_notebooks/` | Undefined fields and nonexistent tables. Either fix against the real gold schema or move them to an `examples/` folder labeled as illustrative. |
| `requirements.txt` | Split by runtime (analytics/dev, dbt, Airflow image) with compatible pins. |

---

## 9. Dependency conflicts (summary)
1. **Python version:** Airflow 2.8.1 needs < 3.12; dbt 1.7.4 needs < 3.12 (distutils); the pinned numpy/pandas/scipy/pyarrow lack 3.13 wheels. **The machine has only 3.12 and 3.13.**
2. **SQLAlchemy:** Airflow 2.8.x needs < 2.0; pandas 2.2 `to_sql` needs ≥ 2.0; the ingest code is written for neither.
3. **dbt adapter:** `dbt-spark` is listed, `dbt-postgres` is needed.
4. **Airflow without its constraints file** is co-installed with dbt-core and great-expectations, so transitive pin clashes are likely. Isolate Airflow in its own image.
5. **Unused heavy dependencies:** `boto3`, `kafka-python`, `spectacles`, `great-expectations`.
6. **Missing dependencies:** `sqlalchemy`, `dbt-postgres`, `jupyter`; Java 17 for PySpark.

**Recommended resolution:** split into separate environments.
- `requirements-analytics.txt` (Python 3.12): pandas, numpy, scipy, scikit-learn, plotly, seaborn, matplotlib, pyarrow, SQLAlchemy ≥ 2, psycopg2-binary, pytest, pyspark 3.5.x.
- `requirements-dbt.txt`: dbt-core + dbt-postgres ≥ 1.8.
- A custom Airflow image, `FROM apache/airflow:2.10.x-python3.12`, installed with the official constraints file.

## 10. Database / schema issues (summary)
- The schema naming mismatch (`public_gold` vs `gold`) breaks every consumer. **Fix:** add a `generate_schema_name` macro that uses the custom schema verbatim, or set `schema:` in the profile accordingly.
- Bronze types are inferred by pandas: `signup_date` / `first_active_date` / `created_date` become TIMESTAMP, which breaks date arithmetic. **Fix:** explicit DDL, or cast in staging (`::date`).
- Tables referenced but never built: `gold.metrics_product_health`, `silver.events_sessionized`, `experiments.experiment_assignments`.
- Columns referenced but absent: `fct_daily_active_users.user_id/signup_date`; the whole `metrics_product_health` column set.
- Airflow metadata shares the analytics DB. **Fix:** a separate `airflow` database or container.
- No primary keys, indexes or constraints on bronze tables. Dedup relies on `ROW_NUMBER` over the full table every run (no incremental models).
- dbt sources declare no columns, so there is no contract between ingestion and dbt.

---

## 11. Exact commands to run the project

### 11a. What can run **today** (Windows, PowerShell or Git Bash, from the project root)
```bash
cd connecthub-product-analytics/connecthub-product-analytics

# Use Python 3.12 explicitly (`python` on PATH is the MS Store stub)
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install pandas numpy scipy scikit-learn plotly seaborn matplotlib pyarrow pytest

# Unit tests: must use `python -m pytest` (bare `pytest` fails to import packages)
.venv/Scripts/python -m pytest tests -q            # expect: 18 passed, 7 failed

# Stats demos
.venv/Scripts/python -m experimentation.power_analysis
.venv/Scripts/python -m experimentation.bayesian_ab

# Synthetic data: WARNING ~40 min and >23 GB RAM at default scale (500K users).
# Reduce NUM_USERS / NUM_WORKSPACES in the script first for a laptop run.
.venv/Scripts/python scripts/generate_synthetic_data.py

# Parquet-based experiment flow (results are SIMULATED)
.venv/Scripts/python -m experimentation.assignment
.venv/Scripts/python -m experimentation.evaluate

# Static dashboard
start index.html          # PowerShell / cmd  (or just open the file in a browser)
```

### 11b. What the full stack **would** require (after the P0 fixes in §12)
```bash
# Prerequisites: Docker Desktop running; Java 17 (for Spark); Python 3.12 (or 3.11 if keeping Airflow 2.8.1)

docker compose up -d postgres                                   # warehouse
python scripts/ingest_events.py --load-postgres                 # bronze load (currently FAILS: SQLAlchemy)
pip install "dbt-core>=1.8" "dbt-postgres>=1.8"
cd dbt_project && dbt deps && dbt build --profiles-dir . && cd ..   # currently FAILS: semantic YAML
spark-submit spark_jobs/sessionize_events.py --local            # needs Java + PySpark 3.5
spark-submit spark_jobs/feature_extraction.py data/events_sessionized.parquet data/feature_usage.parquet
docker compose up -d                                            # Airflow at http://localhost:8080 (needs custom image first)
```

---

## 12. Recommended implementation order

**Phase 0 — Foundation (½ day)**
1. Flatten the nested folder, `git init`, and commit the baseline.
2. Standardize on **Python 3.12**. Split requirements (§9). Add `pyproject.toml` with `[tool.pytest.ini_options] pythonpath = ["."]` and a ruff config.
3. Add `.env.example`. Move all credentials to env vars (`profiles.yml` via `env_var()`, compose via `env_file`).

**Phase 1 — Green baseline (½ day)**
4. Fix `stat_tests.py` / `bayesian_ab.py` to return native `bool` / `float`, so all 25 tests pass and `json.dumps` works.
5. Replace the tautological `test_cohort_engine.py` with real tests against the parquet functions using a tiny fixture dataset.

**Phase 2 — Data generation (1–2 days)**
6. Rewrite the generator: vectorized, `--users` scale flag, behavioral model (ordered funnel, retention decay, plan-based revenue/MRR, NPS survey events, a planted experiment effect keyed off `assign_variant`), agent evals linked to workspaces, deterministic IDs.

**Phase 3 — Warehouse & ingestion (1 day)**
7. Explicit bronze DDL (correct DATE/TIMESTAMP types), `COPY`-based loader with the SQLAlchemy 2.x API, loading of `experiments.experiment_assignments`.
8. Separate the Airflow metadata DB from the analytics DB.

**Phase 4 — dbt (1–2 days)**
9. `dbt-postgres ≥ 1.8`. Add a `generate_schema_name` macro so the schemas are exactly `staging/intermediate/gold`.
10. Delete or replace `semantic/metrics_product_health.yml`.
11. Fix `fct_feature_adoption` (date cast and first-adoption logic) and rewrite `fct_daily_active_users` with a date spine and range joins.
12. Add gold models: `metrics_product_health` (workspace grain) and `fct_experiment_user_metrics` (user grain: activation_14d, revenue_30d, session minutes).
13. Add `schema.yml` with generic tests, docs and source column contracts. Get `dbt build` green.

**Phase 5 — Analytics & experimentation alignment (1 day)**
14. Point all DB paths at the real gold models, with one shared feature-map module.
15. Rewrite `evaluate.py` to use real metrics (no simulation), and implement the session-duration guardrail.
16. Persist assignments to Postgres; use a salted second hash for variant vs traffic bucket.
17. Add a `__main__` CLI to `cohort_engine` (or remove it from the DAG).

**Phase 6 — Spark (1 day)**
18. Decide scope. **Recommended:** local parquet only, with Iceberg dropped from `--local`. Load the Spark silver outputs into Postgres and have `int_sessions` consume real sessions. Add a local-SparkSession pytest fixture. (Optional later: MinIO + Iceberg REST catalog.)

**Phase 7 — Orchestration (1 day)**
19. Custom Airflow image (2.10.x, Python 3.12, constraints file, dbt, Java and PySpark or a SparkSubmit connection). Add an `airflow-init` service, `cwd="/opt/airflow"` on tasks, a real load task, and dbt via `dbt build`. Replace GE with dbt tests, or properly initialize a GE context. Add a DAG import test.

**Phase 8 — Serving: backend + dashboard + AI analyst (2–3 days)**
20. A small API (e.g. FastAPI) exposing read-only gold endpoints (KPIs, retention, funnel, experiment, health).
21. Refactor `index.html` to fetch from the API (keep the visual design). Fix the health chart colors and escape dynamic strings.
22. Rebuild the AI analyst as a server-side endpoint that calls an LLM API with tool use over an allow-listed, read-only SQL interface to gold tables. Keep the key server-side and add conversation history.

**Phase 9 — Polish (1 day)**
23. Fix or relabel LookML and Hex as illustrative. Make the notebooks root-relative (or `%cd ..`).
24. Rewrite README/architecture docs to match what is actually implemented. Replace the placeholder URLs.
25. Add CI (lint + pytest + `dbt parse`/`build` against a Postgres service container).

---

## Appendix A — Static check results

**pytest (Python 3.12 and 3.13):** 18 passed, 7 failed. All failures are `assert np.False_ is False` / `np.True_ is True` in `tests/test_stat_tests.py`.

**ruff** (`--select F,E9,B,PD`): 17 findings. **No syntax errors and no unresolved imports.**
- `analytics/health_scoring.py:33` F841 unused `users`
- `experimentation/evaluate.py:32` F841 unused `events` (confirms the evaluation ignores event data)
- `dags/product_analytics_daily.py:7` F401 unused `PythonOperator`
- `scripts/ingest_events.py:9` F401 unused `datetime`
- `tests/*` F401 unused `pytest` / `numpy` imports
- `scripts/generate_synthetic_data.py:187,192` and `notebooks/01_eda_exploration.ipynb` F541 f-strings without placeholders
- `analytics/activation_funnel.py:83` B905 `zip()` without `strict=`
- PD010 / PD011 pandas style (`.pivot`, `.values`)

**dbt 1.7.4 (dbt-postgres, scratch copy):** `parse` fails on the semantic YAML. With it removed, 11 models parse into `public_staging/intermediate/gold`. `debug` shows profile/project valid and the connection fails (Docker not running).

**docker compose config:** valid; warning that `version` is obsolete.

**pip --dry-run -r requirements.txt:** fails on Python 3.12 and 3.13 (`apache-airflow==2.8.1` requires Python < 3.12).

**Smoke run of the parquet pipeline (2K users, scratch dir):** all functions execute.
- Week-0 retention is about 45–50%.
- The funnel is non-monotonic.
- Health tiers: 0 Healthy, 0 Champion.
- The evaluation is non-deterministic and returned "REVERT — Revenue guardrail failed" on both runs.
- The `evaluate` result is not JSON-serializable.
