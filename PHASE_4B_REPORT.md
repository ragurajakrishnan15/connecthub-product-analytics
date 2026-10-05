# Phase 4B Report: Analytics API Skeleton

**Date:** 2026-10-05 · **Branch:** `phase-4` (on top of Phase 4A `0a35c91`)
**Scope:** `PHASE_4_PLAN.md` §14 steps 2–3:
- the small refactors
- the FastAPI skeleton: settings, read-only pooled database access, errors, request IDs and logging, CORS and security headers, OpenAPI
- `GET /api/health`, `GET /api/health/ready`, `GET /api/meta`
- read-only role provisioning
- Docker wiring (see §7 for why it is included now)

**Not done (by design):**
- the business data endpoints (overview, activation, retention, cohorts, revenue, feature-adoption, experiments, nps, support, customer-health, engagement)
- `index.html` and the frontend
- the AI analyst
- caching and ETag, the OpenAPI snapshot and `docs/api.md` (plan step 5)
- pushing to GitHub

## Bottom line

| Check | Result |
|---|---|
| `pytest` (full suite, incl. end-to-end) | **185 passed, 2 skipped**, 392 s (was 122 + 2). The 63 new API tests all pass; the 2 skips are the usual container-only tests (§9). |
| `ruff check .` | All checks passed |
| `dbt build` | PASS=106 WARN=0 ERROR=0 (no dbt changes in 4B) |
| Refactors change no data | Full pipeline rerun: **34 of 35 relations byte-identical**. The only difference is `analytics.experiment_results`, which now stores `decision_code` (§2). |
| HTTP smoke test (container, real HTTP) | **12/12** checks pass |
| Docker | Image builds (488 MB); `api-init` provisions; `api` is healthy, non-root, holds no owner credentials; Postgres outage and recovery handled without a restart |
| Secrets | No `.env` secret value in any of the 44 changed or new files (this report included), nor in the container logs |

---

## 1. Dependencies

Following the project's convention (top-level pins plus a lock file):
- **New `requirements/api.txt`** with the API's runtime only: `fastapi==0.142.2`, `uvicorn==0.54.0` and `pydantic-settings==2.15.0`, plus the already-pinned `SQLAlchemy`, `psycopg2-binary`, `pandas` and `numpy`.
- **`requirements.txt`** now includes it, so `make setup` installs it into the dev venv.
- **`requirements/constraints-py311.txt`:** regenerated with its own documented `pip freeze` command. The diff is **exactly 7 added lines** (the 3 packages plus 4 transitive ones: `starlette`, `python-dotenv`, `annotated-doc`, `opentelemetry-api`). Every existing pin is unchanged, including `pydantic==2.13.5`, `anyio` and `httpx`.
- **`pip check`:** no broken requirements in the shared venv, Great Expectations 0.18.8 included.

No other frameworks or packages were added.

## 2. Small refactors (plan step 2)

| Change | Why | Verified by |
|---|---|---|
| New `analytics/retention.py`: `retention_table`, `as_of`, `summarize`, `load_retention` moved verbatim from `cohort_engine.py` (which re-exports them); `summarize(..., weeks=SUMMARY_WEEKS)` | The API image has no matplotlib or seaborn; importing `analytics.retention` loads no plotting library (checked) | Existing cohort, CLI, parity and fixture tests unchanged and passing |
| `pipeline.config.database_url(database, user=None, password=None)` | The API connects as its own role; existing callers unchanged | `test_engine_uses_the_api_role_and_a_read_only_session` |
| Experiment registry: `kind` (`ab` \| `aa`) and `hypothesis` fields; `exp_ai_summary_v1` is `kind='aa'` | The A/A flag comes from the registry, not from API code | `/api/meta` tests |
| `evaluate_user_metrics` adds `decision_code` (`SHIP` / `CONTINUE` / `HOLD` / `REVERT`); the decision text is unchanged | Machine-readable verdict for the experiments endpoints | Existing evaluate tests; persisted rows show `decision_code: SHIP` for both experiments |
| `persist_scores` creates index `(snapshot_date, health_score)` | The plan's customer-health list (Phase 4C) sorts by score | Created by the pipeline run |

**Side effect, handled:**
1. `experimentation/experiments.py` is part of the generator fingerprint, so the next pipeline run regenerated the dataset (same seed, same code), reloaded bronze and did a full dbt refresh.
2. I fingerprinted the warehouse before and after that run (`phase4b-run-1`, rc 0, 133 s).
3. 34 of 35 relations are identical. Only `analytics.experiment_results` changed, because of the new `decision_code` key.

## 3. The API (plan step 3)

```
api/
  main.py          create_app(settings): lifespan (engine), middleware, routers, handlers
  __main__.py      python -m api -> uvicorn (factory; workers, host, port from settings)
  settings.py      pydantic-settings; validated at startup; secrets as SecretStr
  db.py            pooled engine as the read-only role; read-only session; get_conn()
  errors.py        RFC 9457 problems; DB-error classification
  middleware.py    request IDs, JSON access log, security headers, 414, HEAD, 500s
  logging.py       JSON-lines formatter (reuses pipeline.log.redact)
  context.py       per-request context (request ID, DB query stats)
  params.py        unknown query parameters -> 400
  security.py      optional X-API-Key auth (constant-time compare)
  provision.py     python -m api.provision: idempotent read-only role + grants
  routers/         health.py, meta.py                  (HTTP only)
  services/        health.py, meta.py                  (composition, no SQL)
  repositories/    system.py, meta.py                  (SQL, bound params only)
  schemas/         common.py (Envelope, ResponseMeta, Problem), health.py, meta.py
```

**Endpoints:**

| Endpoint | Auth | Source | Behavior |
|---|---|---|---|
| `GET /api/health` | open | none (no I/O) | 200 `{status, version, uptime_s}`, `Cache-Control: no-store`. Used by the container healthcheck. |
| `GET /api/health/ready` | open | `SELECT 1`; `pg_class`/`pg_namespace` with `has_*_privilege` for **all 17 relations the planned endpoints read**; `ops.pipeline_runs` | 200 `ready` only if the database answers, every relation exists **and is readable by the API role**, and a fully validated run (final step `validate_analytics` succeeded) exists; otherwise 503 `not_ready`, saying which check failed. 1 s statement timeout, never raises, never cached. |
| `GET /api/meta` | API key if enabled | `gold.fct_daily_active_users`, `gold.metrics_product_health`, `gold.fct_workspace_mrr`, `ops.pipeline_runs`; registry, feature map, health tiers | Envelope `{data, meta}`: data window, health snapshot date, last validated run, plan tiers **with seat prices from billing**, features, experiments (with `kind`), health tiers, API version, dataset label, plus a synthetic-data caveat. 503 `data-not-ready` until the warehouse is built. |

**No hard-coded or fabricated values:**
- Every dimension value comes from its single source of truth: billing, `analytics/features.py`, the experiment registry or `analytics/health_scoring.py`.
- The only timestamp the API produces is `meta.generated_at`.

**Read-only, in three layers:**
1. The role has no write privileges and no access to bronze, staging, intermediate, experiments or `ops.load_state`.
2. The role defaults to `default_transaction_read_only = on`, and the engine re-asserts it per session, together with `statement_timeout` 5 s, `lock_timeout` 2 s and `idle_in_transaction_session_timeout` 10 s.
3. Each request's transaction is always rolled back.

The tests prove layer 1 on its own, inside an explicitly read-write transaction.

**Errors.** Each condition maps to one problem document (`application/problem+json`, `type = urn:connecthub:problem:<slug>`, always with `request_id`):

| Condition | Status | Problem slug |
|---|---|---|
| Unknown route / method | 404 / 405 | `not-found` / `method-not-allowed` |
| Unknown query parameter | 400 | `invalid-parameter` |
| Parameter validation | 422 | `validation-error` |
| Query string > 2 KB | 414 | `uri-too-long` |
| Missing or invalid API key | 401 + `WWW-Authenticate` | `unauthorized` |
| Missing relation (`42P01`) or missing grant (`42501`) | 503 + `Retry-After` | `data-not-ready` (different remediation in `detail`) |
| Connection failure / pool exhausted / lock wait | 503 + `Retry-After` | `database-unavailable` / `pool-exhausted` / `warehouse-busy` |
| Statement timeout (`57014`) | 504 | `query-timeout` |
| Anything else | 500 | `internal-error` |

Bodies never contain SQL, stack traces, DSNs or secrets. The full error is logged, redacted, under the same request ID.

**Request IDs and logging:**
- **Request IDs:** an incoming `X-Request-ID` matching `^[A-Za-z0-9-]{8,64}$` is kept; anything else is replaced by a generated ID. The ID is echoed in the response header, the problem body and every log line.
- **Request log:** one JSON line per request with `method`, `route` (the template), `status`, `duration_ms`, `db_queries`, `db_ms` and `client`. Uvicorn's own access log is disabled.
- **Startup:** the startup line logs the settings with secrets masked.

**Security and CORS:**
- **Headers on every response, errors included:** `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Cross-Origin-Resource-Policy` and `Content-Security-Policy` (`default-src 'none'` on API routes; the docs pages get a CSP that allows their CDN assets). No `Server` header.
- **CORS:** explicit origins only. `*` and `null` are rejected at startup. GET/HEAD/OPTIONS only, no credentials, `X-Request-ID`/`ETag` exposed.
- **Production mode:** `API_ENV=production` requires `API_AUTH_MODE=api_key` and turns docs off unless explicitly enabled.

**OpenAPI:**
- Served at `/api/openapi.json` and `/api/docs` (Swagger) and `/api/redoc` when docs are enabled.
- Tagged, with summaries and the problem responses declared per route.
- The spec documents exactly the 3 Phase 4B paths.

**Configuration:** every setting is in `api/settings.py`, and `.env.example` documents the `API_*` block. When configuration is invalid or missing, `python -m api` exits 2 with one line per problem. Each line names the variable and **never echoes a value**, which is tested with a planted owner secret.

## 4. Provisioning (`python -m api.provision`)

The command runs with the owner credentials and is idempotent: a rerun reports `updated`. It does the following:
- Creates or updates role `API_DB_USER` with LOGIN and NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOREPLICATION / NOBYPASSRLS. The password is quoted with `psycopg2.sql`.
- Sets the role defaults: read-only, and the statement timeout.
- Grants CONNECT on the database and USAGE on `gold`, `analytics` and `ops`.
- Grants SELECT on all tables in `gold` and `analytics`, and on `ops.pipeline_runs` only.
- Adds **`ALTER DEFAULT PRIVILEGES` for the owner** in `gold` and `analytics`.

It refuses to run if the API role would be the owner. On the dev warehouse: 20 readable tables (17 gold, 2 analytics, 1 ops).

## 5. Validation in the three required database states

| State | Readiness | `/api/meta` | Where tested |
|---|---|---|---|
| **Postgres available, warehouse built** | 200 `ready`: 17/17 relations readable, last run `phase4b-run-1`, `data_end` 2025-12-31 | 200, values equal to direct SQL (window, snapshot, tiers and prices, run ID, step count and duration) | `test_api_warehouse.py`; container smoke test |
| **Warehouse not built** (fresh database, role provisioned) | 503: database ok, 16 relations missing, pipeline not ok, `data_end` null | 503 `data-not-ready` ("run the pipeline") | `test_not_built_warehouse_is_not_ready` |
| **Relation present but not readable** (grant revoked) | 503, relation listed under `not_readable` | 503 `data-not-ready` ("run python -m api.provision") | `test_unreadable_relation_is_reported_not_raised` |
| **Postgres unavailable** | 503: `database.ok = false`, `error: database-unavailable` | 503 `database-unavailable` + `Retry-After: 5` | `test_api_http.py` (closed port); container with Postgres stopped |
| **Configuration missing or invalid** | the process does not start: exit 2, message per variable, no values | — | `test_api_settings.py` (22 tests incl. subprocess) |

## 6. Tests added (63, all passing)

| File | Kind | Tests | Covers |
|---|---|---|---|
| `tests/api/test_api_settings.py` | unit | 22 | Required password; safe defaults; comma lists from env; `*` / `null` / malformed origins rejected; production rules; API-key rules; invalid values; messages and summary never contain secrets; engine URL uses the API role with read-only session options; `python -m api` fails cleanly |
| `tests/api/test_api_http.py` | unit (no DB) | 29 | Liveness; HEAD; readiness and meta with Postgres down; 404/405/400/414/500 problems (500 body generic, log redacted); DB-error → slug mapping (7 cases) and their statuses / `Retry-After`; request ID generated, propagated or replaced; one JSON log line per request; security headers on 200/404/405/503; docs CSP; CORS allow/deny incl. `null` and preflight; API-key mode; OpenAPI contents; docs off in production |
| `tests/api/test_api_warehouse.py` | integration | 12 | Ready and meta on the built warehouse vs SQL; DB query stats in logs; app sessions read-only with timeouts; role reads only gold, analytics and `ops.pipeline_runs`; role read-only by default **and** unable to write in a read-write transaction; role attributes; **grants survive `dbt run --full-refresh`** (table swap); provisioning idempotent and refuses the owner; not-built and unreadable-relation states |

The test helpers are in `tests/api/api_testlib.py`. There is no conftest or `__init__.py`, so the test directory can't shadow the `api` package. **No existing test was changed or removed.**

## 7. Docker

**Why it is in 4B:** the plan has Docker as step 6, but provisioning runs as the compose service `api-init` (plan §8.6), and you asked for Docker validation. It is wiring only; nothing endpoint-specific.

**Files:**
- **`docker/api/Dockerfile`:**
  - Base image `python:3.11-slim-bookworm`, installing only `requirements/api.txt` from the lock file.
  - Copies only the modules the API imports.
  - Non-root `app` user (uid 10001).
  - The healthcheck is **liveness only**, so a stopped warehouse never restarts the API.
- **`docker-compose.yml`:**
  - `api-init` (one-shot provisioning with owner credentials).
  - `api` (only `API_DB_*` credentials; `127.0.0.1:${API_PORT:-8000}`; 2 workers; starts after `api-init`, not after readiness).
- **`Makefile`:** `api`, `api-role`, `api-up`, `api-test`.

**Results:**

| Check | Result |
|---|---|
| `docker compose config` | valid. The `api` env has no `POSTGRES_USER` / `POSTGRES_PASSWORD`; `api-init` does. |
| `docker compose build api-init` | OK in 38 s, image **488 MB** (base layers reused) |
| `api-init` | exit 0: `{"role": "connecthub_api", "action": "updated", "readable_tables": 20}` |
| `api` container | healthy; user `app` (uid 10001); 2 workers started; startup log shows `api_db_password: "***"`; the password value appears nowhere in the logs |
| HTTP smoke test (host → `127.0.0.1:8002`) | **12/12**: health 200; ready 200 (`phase4a-run-2` at the time); meta 200 envelope; HEAD empty; `/api/overview` 404 problem (not built yet); 405; unknown parameter 400; request ID propagated; security headers; CORS allow/deny; OpenAPI paths; Swagger UI |
| Postgres stopped while the API runs | liveness 200 and the container stays healthy; ready 503 `database-unavailable`; meta 503 problem with `Retry-After: 5` |
| Postgres restarted | the **same API process** is ready again and meta is 200, with no restart (`pool_pre_ping` discards dead connections) |

**Port:** 8000 on this machine is held by another project's container (`enterpriseiq-app`, which also holds 8001). I didn't touch it. The plan's default of 8000 is kept in code, compose and `.env.example`; only the local, git-ignored `.env` sets `API_PORT=8002`.

## 8. Files changed

**Added (33):**
- `api/`: 25 files (§3).
- `analytics/retention.py`.
- `docker/api/Dockerfile`.
- `requirements/api.txt`.
- `tests/api/`: `api_testlib.py`, `test_api_settings.py`, `test_api_http.py`, `test_api_warehouse.py`.
- `PHASE_4B_REPORT.md`.

**Modified (11):**

| File | Change |
|---|---|
| `analytics/cohort_engine.py` | Pure functions moved to `retention.py`, re-exported |
| `analytics/health_scoring.py` | Score index in `persist_scores` |
| `experimentation/evaluate.py` | `decision_code` |
| `experimentation/experiments.py` | `kind`, `hypothesis` |
| `pipeline/config.py` | `database_url(user=, password=)` |
| `docker-compose.yml` | `api-init`, `api` services |
| `Makefile` | API targets |
| `.env.example` | `API_*` block (placeholders only) |
| `pyproject.toml` | Filter for Starlette's TestClient deprecation warning, matched to its exact message |
| `requirements.txt` | Includes `requirements/api.txt` |
| `requirements/constraints-py311.txt` | +7 pins |

**Local only (git-ignored):**
- `.env` gained `API_DB_USER`, a generated `API_DB_PASSWORD` and `API_PORT=8002`.

**Not touched:**
- `index.html`
- the DAG
- the dbt project
- every Phase 1–4A test

## 9. Skipped tests

| Test | Why |
|---|---|
| `tests/test_dag.py` | Airflow isn't in the dev venv; it runs in the Airflow container (unchanged since Phase 3) |
| `tests/test_spark_jobs.py` | pyspark isn't in the dev venv; it runs in the Spark container (unchanged since Phase 3) |

Nothing else was skipped: the integration and end-to-end tests ran against live PostgreSQL.

## 10. Deviations from the plan

1. **No dbt `+grants`.** The plan (§3) proposed `+grants` in `dbt_project.yml`. Instead, provisioning sets `ALTER DEFAULT PRIVILEGES` for the owner in `gold` and `analytics`, which covers every table dbt or the analytics step creates, swaps or fully refreshes. `+grants` would also make **every dbt build fail** in a database where the role doesn't exist: the e2e and fixture test databases, fresh clones, the Airflow container. A test proves the grants survive a `dbt run --full-refresh`.
2. **Problem `type`** is `urn:connecthub:problem:<slug>` instead of the plan's example `https://connecthub.dev/problems/...`, a domain this project doesn't own.
3. **Docker wiring moved from step 6 to 4B** (see §7).
4. **Added files the plan's layout didn't list:** `api/security.py`, `api/context.py`.
5. **Deferred, as the plan orders them:**
   - response caching, ETag and 304 (step 5); `/api/meta` is not cached yet
   - the OpenAPI snapshot test and `docs/api.md` (step 5)
   - `API_DASHBOARD_PATH`, serving `index.html` (Phase 5)

## 11. Remaining risks

1. **Disk:** C: has **2.6 GB free**, and Docker holds about 24 GB of images and 19.6 GB of build cache, some of it reclaimable. Further image builds could fail; I didn't prune anything belonging to other projects.
2. **Starlette 1.7 is a new major line.** Its TestClient now prefers `httpx2`; the pinned `httpx` works and the warning is filtered by exact message. A future upgrade may require `httpx2`.
3. **The Airflow image is stale.** It bakes the project code in and needs `docker compose build` to pick up the 4A models and the 4B refactors. Its old code still runs, because nothing it calls was removed.
4. **Port conflicts on this machine:** 8000/8001 are used by another project (worked around with the local `.env` only).
5. **`opentelemetry-api`** arrives transitively with FastAPI 0.142. It is unused, but now part of the lock file.
6. **Not yet implemented:** rate limiting (left to a reverse proxy, per the plan) and the cache.

## 12. Next step (not started)

Phase 4C = plan §14 step 4: the data endpoints, one at a time, each with its repository, schema, parity test and docs entry:
1. overview
2. engagement
3. activation
4. retention
5. cohorts
6. revenue
7. feature-adoption
8. experiments
9. experiments/{id}
10. nps
11. support
12. customer-health
