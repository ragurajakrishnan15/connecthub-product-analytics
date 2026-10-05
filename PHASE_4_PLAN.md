# Phase 4 Plan: Product-Facing Analytics API

**Date:** 2026-10-05 · **Base:** `main` @ `5e03bc4` (tag `v3.0`) · **Branch:** `phase-4`
**Goal:** A production-quality FastAPI backend that serves business-ready metrics from the real warehouse.
**Rules:**
- No hard-coded metrics, mock responses or fake analytics.
- Every number comes from dbt/analytics tables that the pipeline builds and Great Expectations validates.

**Out of scope:**
- Changing `index.html`. This plan only defines how it will consume the API (§12).
- The AI analyst.
- Cloud deployment.

---

## 0. What exists today (inspection)

The schema below comes from the code that creates it:
- bronze DDL in `scripts/ingest_events.py`
- dbt models
- the `CREATE TABLE` statements in `analytics/health_scoring.py`, `experimentation/evaluate.py` and `pipeline/`

Docker was not running during planning, so **step 0 of the work (§14)** confirms these relations live through `information_schema`.

`great_expectations/` no longer exists: Phase 3 replaced it with `quality/`.

### 0.1 Relations the API can use

| Relation | Grain | Rows at 10K users | Scales with | Useful for |
|---|---|---|---|---|
| `gold.fct_daily_active_users` | day | 365 | days | DAU, WAU (7d), MAU (28d), active workspaces |
| `gold.fct_retention_cohorts` | cohort week × week N (0–12) | ~650 | weeks | retention matrix (percent) |
| `gold.fct_feature_adoption` | feature × day since signup (0–90) | ~640 | features | cumulative adoption curves (percent) |
| `gold.fct_workspace_mrr` | workspace × month | ~6.4K | workspaces | MRR, seats, movement |
| `gold.fct_agent_evaluations` | AI-handled call | ~16K | users | AI resolution, escalation, CSAT, handle time |
| `gold.fct_experiment_user_metrics` | experiment × user | ~14K | users | per-user experiment metrics |
| `gold.fct_experiment_assignments` | experiment × user | ~14K | users | assignments |
| `gold.metrics_product_health` | workspace (latest snapshot) | 1K | workspaces | health inputs |
| `intermediate.int_activation_funnel` | user | 10K | users | 14-day milestones and first-milestone dates |
| `intermediate.int_feature_usage` | user × workspace × day × feature | ~249K | events | feature usage |
| `intermediate.int_sessions` | session | ~147K | events | session duration |
| `staging.stg_nps_responses` (view) | response | ~2.1K | users | NPS |
| `staging.stg_events` | event | ~782K | events | support tickets (`support.ticket_created` / `_resolved`) |
| `analytics.workspace_health_scores` | snapshot × workspace | 1K per snapshot | workspaces | health score 0–100, tier, inputs |
| `analytics.experiment_results` | experiment | 2 | experiments | persisted evaluation incl. `result_json` (JSONB) |
| `ops.pipeline_runs` | run × step | small | runs | data freshness, last successful run |

### 0.2 Business logic to reuse, not re-implement

| Logic | Where | How the API uses it |
|---|---|---|
| Retention cells, completeness cut-off, pooled week-N | `analytics/cohort_engine.py` (`load_retention`, `as_of`, `summarize`) | Called directly. `summarize` gains an optional `weeks` argument (default stays `(1, 4, 8, 12)`). |
| Health tiers and weights | `analytics/health_scoring.py` (`TIER_BINS`, `TIER_LABELS`, `WEIGHTS`) | Imported for metadata and histogram bands; scores are read from the persisted table. |
| Experiment registry | `experimentation/experiments.py` | Experiment list, windows, eligibility, traffic. |
| Experiment evaluation and decision | `experimentation/evaluate.py` | **Not re-run per request.** The API serves `analytics.experiment_results`, which is computed by the same code and validated by GE. |
| Feature map | `analytics/features.py` | Feature list and AI-feature set. |
| DB URL and secret redaction | `pipeline/config.py`, `pipeline/log.py` | Same URL builder, extended to take a user and password. Same `redact()` and JSON formatter. |
| Metric definitions | `docs/metric-definitions.md` | Every response field maps to an entry. New metrics are added there first. |

### 0.3 Gaps found during inspection

These shape the design:

1. **Gold is missing aggregates for NPS, support, AI-agent trends, activation by signup date, monthly revenue by plan and monthly feature usage.** Computing them per request would scan `stg_events` (40M rows at 500K users) or `int_feature_usage`. → New dbt gold serving models (§3).
2. **`fct_retention_cohorts` omits zero cells and includes incomplete ones.**
   - The `LEFT JOIN … WHERE a.activity_week >= …` drops weeks with 0 active users.
   - The latest cohorts include weeks that have not ended yet.
   - `cohort_engine.as_of` already handles completeness; the API must also fill complete-but-missing cells with 0.
3. **`int_activation_funnel` flags are independent and not window-censored.**
   - Users who signed up in the last 14 days of the data look unactivated.
   - A strict funnel needs `call`, `call ∧ ai`, `call ∧ ai ∧ invite`.
   - The new activation model adds both.
4. **`fct_feature_adoption` is right-censored.** Its denominator is all users, including users with fewer than D days of history, so late-day adoption is understated. The API labels this in `meta.caveats`; fixing the model is listed as optional (§3, model 10).
5. **There is no ticket ID.** `support.ticket_resolved` events can't be paired with tickets, so resolution time and backlog are **not derivable**. The API reports counts and ratios only and says so.
6. **Plan tier changes over time.** `stg_workspaces.plan_tier` is the current plan; `stg_subscriptions.plan_tier` is the billed plan per month. Serving models use the **billed plan for the month of the activity** consistently.
7. **`index.html` is fully hard-coded.** It also shows panels that have no data source (§12.3).
8. **The synthetic data covers calendar 2025.** Default date windows must be relative to the **data end date** (`MAX(event_date)`), never the wall clock. Phase 3 applied the same "no clock" rule to assignment.

---

## 1. Architecture

```
index.html (Phase 5) ──HTTP GET──▶ FastAPI app (api/) ──read-only role, pooled──▶ PostgreSQL
                                     │                                             gold.*        (dbt, incl. new serving models)
                                     │ routers → services → repositories           analytics.*   (health scores, experiment results)
                                     │ reuse: analytics/, experimentation/         ops.pipeline_runs (freshness / data version)
                                     │ cache keyed by data version
pipeline (unchanged order) ── generate → assign → ingest → validate → dbt build (+ serving models) → validate → analytics → validate
```

**Principles:**
1. **Metric definitions live in dbt (and the existing Python analytics).**
   - The API composes them and may compute ratios from additive sums in one module (`api/metrics.py`).
   - Each ratio cites its `docs/metric-definitions.md` entry and has a parity test.
2. **Serving tables are additive (counts and sums) at daily or monthly grain.** Any date range re-aggregates exactly, and ratios are computed from sums, never averaged.
   - Their size depends on calendar days × plan tiers, **not on users**.
   - So endpoint latency stays roughly flat from 10K to 500K users.
3. **The API reads only `gold`, `analytics` and `ops.pipeline_runs`**, never bronze, staging or intermediate.
4. **Read-only end to end:** a dedicated database role, read-only transactions, and GET-only routes.
5. **Explicit nulls:** an undefined ratio (0 denominator) or a suppressed metric (too few responses) is `null` with a reason, never `0`.
6. **No clock:** defaults derive from the data window (`/api/meta`).

**Synchronous SQLAlchemy 2.0 + psycopg2, not async.** The project already uses this stack, `pipeline/config.py` builds its URLs, and pandas reuse needs it. FastAPI runs `def` endpoints in its threadpool. Queries are small aggregates over serving tables, so async drivers would add a second driver and code path without measurable benefit at this scale.

---

## 2. Package layout

```
api/
  __init__.py          version
  __main__.py          `python -m api` → uvicorn (host/port/workers from settings)
  main.py              create_app(): lifespan (engine create/dispose), middleware, routers, exception handlers
  settings.py          pydantic-settings Settings (env-driven, validated at startup)
  db.py                engine + pool, get_conn() dependency (read-only txn, timeouts), error mapping
  cache.py             data-version lookup + TTL cache + ETag helpers
  errors.py            problem+json (RFC 9457) exception types and handlers
  logging.py           JSON request logging (reuses pipeline.log.JsonFormatter / redact)
  middleware.py        request ID, timing, security headers
  params.py            shared query-parameter models (DateRange, Granularity, PlanTier, Pagination)
  metrics.py           ratio/derived-metric formulas (single place; cites metric-definitions.md)
  provision.py         `python -m api.provision`: idempotent read-only role + grants (run with owner creds)
  schemas/             Pydantic response models per domain (common.py: Envelope, Meta, Kpi, Series…)
  repositories/        SQL per domain (sqlalchemy.text with bound params only)
  services/            composition: repositories + reused analytics functions → response models
  routers/             health, meta, overview, engagement, activation, retention, cohorts, revenue,
                       feature_adoption, experiments, nps, support, customer_health
docker/api/Dockerfile
requirements/api.txt
```

---

## 3. New dbt gold serving models

These are plain `table` materializations, rebuilt on every `dbt build`. Each has a `schema.yml` entry with a grain-uniqueness test and a GE gold contract (§9).

"Plan" always means the billed plan in `stg_subscriptions` for the month of the activity, falling back to `stg_workspaces.plan_tier` when there is no subscription row.

| # | Model | Grain | Columns (additive) | Built from | Endpoint |
|---|---|---|---|---|---|
| 1 | `fct_activation_daily` | signup_date × plan_tier | signups, placed_first_call, used_ai_feature, invited_team_member, call_and_ai, activated_14d, window_14d_complete | `int_activation_funnel`, `stg_users`, `stg_subscriptions`, `stg_events` (data end) | activation, overview |
| 2 | `fct_activation_milestone_days` | signup_date × plan_tier × milestone × days_to_milestone (0–14) | users | `int_activation_funnel` (first_* dates; `fully_activated` = GREATEST of the three) | activation (exact medians) |
| 3 | `fct_revenue_monthly` | month_start × plan_tier | mrr_usd, paying_workspaces, billed_seats, new/expansion/contraction/churned/reactivation `_mrr_usd` (= Σ `mrr_change_usd` per movement), workspace counts per movement | `fct_workspace_mrr` | revenue, overview |
| 4 | `fct_nps_daily` | response_date × plan_tier | responses, promoters, passives, detractors, score_sum | `stg_nps_responses` | nps, overview |
| 5 | `fct_support_daily` | event_date × plan_tier | tickets_created, tickets_resolved, active_users (distinct per day → summed = active user-days) | `stg_events` | support |
| 6 | `fct_agent_performance_daily` | call_date × plan_tier × call_type | calls, ai_resolved, escalated, human_handled, csat_sum, csat_count, handle_time_seconds_sum | `fct_agent_evaluations` | support, overview |
| 7 | `fct_feature_usage_monthly` | month_start × feature_name | active_workspaces, active_users | `int_feature_usage` | feature-adoption |
| 8 | `fct_activity_monthly` | month_start | active_users, active_workspaces, ai_active_users, ai_active_workspaces | `stg_events`, `int_feature_usage` | feature-adoption, overview |
| 9 | `fct_experiment_activation_curve` | experiment × variant × day_since_signup (0–14) | users_in_window, activated_cumulative | `fct_experiment_user_metrics`, `int_activation_funnel` | experiments/{id} |
| 10 *(optional)* | `fct_retention_by_week1_features` | features_adopted_week1 × plan_tier | users, retained_week4 (complete windows only) | `stg_users`, `int_feature_usage`, `stg_events` | retention (backs an existing dashboard panel) |

**New dbt tests:**
- **Grain uniqueness** for every model.
- **Funnel monotonicity:** `activated_14d ≤ call_and_ai ≤ placed_first_call ≤ signups`.
- **NPS reconciliation:** `promoters + passives + detractors = responses`.
- **Agent reconciliation:** `ai_resolved + escalated + human_handled = calls`.
- **MRR reconciliation:** month MRR − previous month MRR = Σ movement amounts, at the all-plans level. Per tier it doesn't hold when a workspace changes tier.
- **Totals parity:** Σ `fct_activation_daily.signups` = `COUNT(stg_users)`, and Σ `fct_revenue_monthly.mrr_usd` = Σ `fct_workspace_mrr.mrr_usd`.

**Cost:**
- Models 5 and 8 scan `stg_events` once per build, like `fct_daily_active_users`. At 10K users that adds a few seconds to the build.
- They can become incremental later, following the `stg_events` pattern.
- The e2e idempotency test and `verify-incremental` cover them automatically. The fingerprinted relation count goes from 26 to about 35.

**Grants:** `dbt_project.yml` gains `+grants: {select: ["{{ env_var('API_DB_USER', 'connecthub_api') }}"]}` for `gold`. dbt re-applies grants after each rebuild, which matters because table swaps drop the old relation's grants.

---

## 4. API conventions

**Envelope.** Every data endpoint returns:
```json
{
  "data": { "...": "endpoint-specific" },
  "meta": {
    "as_of": "YYYY-MM-DD",            // data end date the numbers describe
    "data_start": "YYYY-MM-DD", "data_end": "YYYY-MM-DD",
    "effective_range": {"start": "...", "end": "..."},   // after defaults/clamping, if ranged
    "filters": {"plan_tier": null},
    "sources": ["gold.fct_nps_daily"],
    "data_version": "20261005T005735-ab12cd",            // last successful pipeline run_id
    "generated_at": "ISO-8601 UTC",
    "definitions": "docs/metric-definitions.md#nps",
    "caveats": ["..."]                                   // e.g. censoring, synthetic data, multiple testing
  }
}
```

**Value conventions:**
- **Rates and shares** are fractions in [0, 1]. Fields end in `_rate` or `_share`. Gold tables store retention and adoption as percent, so repositories divide by 100 in one place.
- **Money** is USD, 2 decimals, in fields ending `_usd`. `NUMERIC` → `Decimal` → `float` happens at the schema boundary.
- **Dates** are ISO `YYYY-MM-DD`. Weeks start on Monday, matching PostgreSQL `DATE_TRUNC('week')`.
- **KPI object:**
  ```
  {value, previous_value, change_abs, change_rel, period: {start, end}, previous_period: {...}, unit, definition}
  ```
  `change_rel` is null when `previous_value` is 0 or null.
- **Series:** `{granularity, points: [{period_start, period_end, is_complete, ...metrics}]}`. `is_complete` is false for a period extending past `data_end`.
- **Naming and shape:** keys are snake_case, and no endpoint returns raw table rows.

**Shared parameters** (`api/params.py`, Pydantic query models with `extra="forbid"`, so unknown parameters return 400):

| Parameter | Type | Rule |
|---|---|---|
| `start`, `end` | date | Inclusive. `start ≤ end`. The range must overlap `[data_start, data_end]`. A partial overlap is clamped and reported in `meta.effective_range`; no overlap returns 400. Max span 400 days. |
| `start_month`, `end_month` | `YYYY-MM` | Same rules at month grain |
| `granularity` | `day \| week \| month` | Default per endpoint; `day` is allowed only for spans ≤ 120 days |
| `plan_tier` | `Free \| Essentials \| Professional \| Enterprise` | Optional filter |
| `limit` / `offset` | int | `1 ≤ limit ≤ 100` (default 25), `0 ≤ offset ≤ 10000` |

Default windows are anchored at `data_end`.

---

## 5. Endpoints

Each endpoint below lists purpose, source, parameters, response schema, validation, the expected query and performance considerations.

**Common to every endpoint:**
- SQL values are always bound parameters.
- Optional filters use the fixed fragment `(CAST(:plan_tier AS TEXT) IS NULL OR plan_tier = :plan_tier)`, so SQL text is never built from user input.
- Performance figures are **targets to verify** in §14. Every endpoint reads ≤ a few thousand rows from tables whose size does not grow with users, except where noted.

### 5.1 `GET /api/health` (liveness) and `GET /api/health/ready` (readiness)

| | |
|---|---|
| **Purpose** | `/health`: the process is up (Docker healthcheck, restarts). `/health/ready`: the API can serve correct data (load balancers, `depends_on`). |
| **Source** | `/health`: none (no DB). `/ready`: `SELECT 1`; `to_regclass()` for every required relation; `ops.pipeline_runs`. |
| **Parameters** | None |
| **Response** | `/health` → `{status: "ok", version, uptime_s}`. `/ready` → `{status: "ready" \| "not_ready", checks: {database: {ok, latency_ms}, relations: {ok, missing: []}, pipeline: {ok, last_success_run_id, last_success_at}}, data_end}` with HTTP 200 or 503. |
| **Validation** | None |
| **Query** | `SELECT to_regclass(:r) IS NOT NULL` per relation, batched as `SELECT r, to_regclass(r) IS NOT NULL FROM unnest(CAST(:rels AS text[])) r`. Last successful run: `SELECT run_id, started_at FROM ops.pipeline_runs WHERE step = 'validate_analytics' AND status = 'success' ORDER BY started_at DESC LIMIT 1`. |
| **Performance** | `/health` does no I/O. `/ready` uses a 1 s statement timeout and is not cached. A missing relation → 503 with `missing` listed, meaning the warehouse is not built and the pipeline should be run. |

### 5.2 `GET /api/meta`

| | |
|---|---|
| **Purpose** | Everything the dashboard needs to set itself up without literals: data window, snapshot date, freshness, dimension values, experiment list, tier thresholds, API version. It replaces the fake "Live / Oct 2025 — Sep 2026" header. |
| **Source** | `gold.fct_daily_active_users` (min/max date); `gold.metrics_product_health` (snapshot); `ops.pipeline_runs`; `experimentation.experiments.EXPERIMENTS`; `analytics.features`; `health_scoring.TIER_BINS/TIER_LABELS` |
| **Parameters** | None |
| **Response** | `{data_start, data_end, health_snapshot_date, last_successful_run: {run_id, started_at, duration_s}, plan_tiers: [...], features: [{name, is_ai}], experiments: [{experiment_id, start, end}], health_tiers: [{label, min, max}], api_version, dataset: {synthetic: true}}` |
| **Validation** | None |
| **Query** | `SELECT MIN(event_date), MAX(event_date) FROM gold.fct_daily_active_users`; `SELECT MAX(snapshot_date) FROM gold.metrics_product_health`; the run query from 5.1 plus `SUM(duration_s)` for that `run_id`. |
| **Performance** | Cached by data version. `dataset.synthetic` comes from settings (`API_DATASET_LABEL`). |

### 5.3 `GET /api/overview`

| | |
|---|---|
| **Purpose** | The headline KPI cards, each with a defined current and previous period. |
| **Source** | `fct_revenue_monthly`, `fct_daily_active_users`, `fct_retention_cohorts` (via `cohort_engine`), `fct_activity_monthly`, `fct_activation_daily`, `fct_nps_daily`, `fct_agent_performance_daily`, `analytics.workspace_health_scores` |
| **Parameters** | None. Always the latest data; history lives in the domain endpoints. |
| **Response** | `{kpis: {mrr_usd, dau, week4_retention_rate, ai_feature_adoption_rate, activation_rate_14d, nps, ai_resolution_rate}: Kpi, health_tiers: {Critical, At Risk, Healthy, Champion: {workspaces, share}}, experiments: {total, by_decision: {...}}}` |
| **Validation** | None |
| **Performance** | About 8 small queries, run sequentially on one pooled connection. Target < 150 ms uncached, < 10 ms cached. |

**KPI period definitions** (also returned in each KPI's `definition`):

| KPI | Current | Previous |
|---|---|---|
| MRR | latest complete month | month before |
| DAU | `data_end` | `data_end − 28 d` (same weekday) |
| Week-4 retention | pooled as of `data_end` | pooled as of `data_end − 28 d` |
| AI feature adoption | latest complete month (`ai_active_workspaces / active_workspaces`) | month before |
| Activation rate 14d | signups in `[data_end − 43, data_end − 14]` (30 days, complete windows) | the 30 days before |
| NPS | `(data_end − 90, data_end]` | the 90 days before |
| AI resolution rate | last 30 days | prior 30 |

**Expected queries** (two examples; the others follow the same pattern):
```sql
-- MRR, latest complete month and the one before
WITH m AS (
  SELECT month_start, SUM(mrr_usd) AS mrr_usd
  FROM gold.fct_revenue_monthly
  WHERE (month_start + INTERVAL '1 month' - INTERVAL '1 day')::date <= :data_end
  GROUP BY month_start)
SELECT month_start, mrr_usd FROM m ORDER BY month_start DESC LIMIT 2;

-- NPS for two consecutive 90-day windows
SELECT CASE WHEN response_date > CAST(:data_end AS date) - 90 THEN 'current' ELSE 'previous' END AS period,
       SUM(responses) AS n, SUM(promoters) AS p, SUM(detractors) AS d
FROM gold.fct_nps_daily
WHERE response_date > CAST(:data_end AS date) - 180
GROUP BY 1;
```
Week-4 retention reuses `cohort_engine.summarize(as_of(cells, d), d)['pooled_retention_pct']['week_4'] / 100`.

### 5.4 `GET /api/engagement` (added: backs the DAU chart)

| | |
|---|---|
| **Purpose** | Active-user trend: DAU, WAU, MAU, active workspaces, stickiness. |
| **Source** | `gold.fct_daily_active_users` |
| **Parameters** | `start`, `end` (default: the year ending at `data_end`), `granularity` (default `week`) |
| **Response** | `{summary: {dau, wau_7d, mau_28d, stickiness_rate (DAU/MAU), active_workspaces} (at period end), series: Series[{period_start, period_end, is_complete, avg_dau, wau_7d_end, mau_28d_end, avg_active_workspaces, stickiness_rate}]}` |
| **Validation** | Shared date rules. `day` granularity is limited to ≤ 120 days. |
| **Query** | ↓ |
| **Performance** | 365 rows maximum. Target < 30 ms. |

```sql
SELECT DATE_TRUNC(:grain, event_date)::date AS period_start,
       MAX(event_date) AS last_day,
       AVG(dau) AS avg_dau, AVG(active_workspaces) AS avg_active_workspaces,
       (ARRAY_AGG(wau_7d  ORDER BY event_date DESC))[1] AS wau_7d_end,
       (ARRAY_AGG(mau_28d ORDER BY event_date DESC))[1] AS mau_28d_end
FROM gold.fct_daily_active_users
WHERE event_date BETWEEN :start AND :end
GROUP BY 1 ORDER BY 1;
```
`:grain` is an enum value mapped to a literal from an allow-list. It is the only non-value part of any query, and it never comes from raw input.

### 5.5 `GET /api/activation`

| | |
|---|---|
| **Purpose** | The 14-day activation funnel, rate trend, time to each milestone and stage mix, for a signup-date range. |
| **Source** | `gold.fct_activation_daily`, `gold.fct_activation_milestone_days` |
| **Parameters** | `start`, `end` (signup dates; default: the 90 days ending `data_end − 14`), `plan_tier`, `granularity` (default `week`), `include_incomplete` (bool, default false) |
| **Response** | See below |
| **Validation** | Shared rules. If `end > data_end − 14` and `include_incomplete=false`, the effective end becomes `data_end − 14` and a caveat is added. |
| **Query** | ↓ |
| **Performance** | Rows = days × 4 tiers, at most ~1.5K, and ~22K for milestone days. Target < 60 ms. |

Response:
```
{funnel: [{stage: "signed_up"|"placed_first_call"|"call_and_ai"|"activated_14d",
           users, rate_of_signups, rate_of_previous}],
 milestones: {placed_first_call_rate, used_ai_feature_rate, invited_team_member_rate},   // independent
 activation_rate_14d,
 time_to_milestone: [{milestone, median_days, p75_days, users_reached}],               // among users reaching it within 14d
 series: Series[{signups, activated_14d, activation_rate_14d}]}
```

```sql
SELECT SUM(signups) s, SUM(placed_first_call) c, SUM(call_and_ai) ca, SUM(activated_14d) a,
       SUM(used_ai_feature) ai, SUM(invited_team_member) inv
FROM gold.fct_activation_daily
WHERE signup_date BETWEEN :start AND :end
  AND (CAST(:plan_tier AS TEXT) IS NULL OR plan_tier = :plan_tier)
  AND (:include_incomplete OR window_14d_complete);

-- exact median / p75 from the integer-day histogram
WITH h AS (
  SELECT milestone, days_to_milestone, SUM(users) n
  FROM gold.fct_activation_milestone_days
  WHERE signup_date BETWEEN :start AND :end
    AND (CAST(:plan_tier AS TEXT) IS NULL OR plan_tier = :plan_tier)
  GROUP BY 1, 2),
c AS (
  SELECT *, SUM(n) OVER (PARTITION BY milestone ORDER BY days_to_milestone) cum,
            SUM(n) OVER (PARTITION BY milestone) total
  FROM h)
SELECT milestone, total,
       MIN(days_to_milestone) FILTER (WHERE cum >= 0.50 * total) median_days,
       MIN(days_to_milestone) FILTER (WHERE cum >= 0.75 * total) p75_days
FROM c GROUP BY milestone, total;
```

### 5.6 `GET /api/retention`

| | |
|---|---|
| **Purpose** | The retention **summary**: pooled week-N curve, headline weeks and trend by cohort month. |
| **Source** | `gold.fct_retention_cohorts` via `cohort_engine.load_retention`, `as_of` and `summarize` (generalized to all weeks) |
| **Parameters** | `as_of` (date, default `data_end`, must be in the data window), `cohort_start`, `cohort_end` (optional cohort-week filter) |
| **Response** | See below |
| **Validation** | `as_of` in the data window. Cohort filters are snapped to Mondays, and the snapping is reported in `meta`. |
| **Query** | `cohort_engine.load_retention(engine=api_engine)`: one query, ~650 rows at any user scale. Pooling, completeness and zero-filling are in reused Python. |
| **Performance** | Target < 80 ms uncached; the pandas work is about 1 ms. Cached by `(as_of, cohort range, data_version)`. |

Response:
```
{pooled_curve: [{week, retention_rate, cohorts_included, users_included}],      // weeks 0–12, complete cohorts only
 headline: {week_1, week_4, week_8, week_12},
 by_cohort_month: [{month, cohort_users, week_1_rate, week_4_rate}],
 by_week1_features?: [...] (only if optional model 10 is built; otherwise omitted, not faked)}
```

### 5.7 `GET /api/cohorts`

| | |
|---|---|
| **Purpose** | The full cohort × week **matrix** for the heatmap: complete cells only, zero-filled. |
| **Source** | Same as 5.6 |
| **Parameters** | `as_of` (default `data_end`), `cohort_start`, `cohort_end` (default: last 26 cohorts), `weeks` (1–12, default 12) |
| **Response** | `{weeks: [0..N], cohorts: [{cohort_week, cohort_size, cells: [{week, active_users, retention_rate} \| null]}]}`. `null` means the cell is not complete as of `as_of`; a complete cell missing from gold has `active_users = 0`. |
| **Validation** | At most 53 cohorts per request; `weeks` in 1–12. |
| **Query** | Same as 5.6, then `as_of` and pivot. Cohort sizes come from the week-0 rows. |
| **Performance** | ~650 rows. Target < 80 ms. |

### 5.8 `GET /api/revenue`

| | |
|---|---|
| **Purpose** | MRR trend, plan mix, paying workspaces, ARPA, seats and the MRR movement waterfall. |
| **Source** | `gold.fct_revenue_monthly` |
| **Parameters** | `start_month`, `end_month` (default: the 12 months ending at the latest month), `plan_tier`, `group_by` (`none` \| `plan_tier`, default `plan_tier`) |
| **Response** | See below |
| **Validation** | Month rules. `group_by=plan_tier` combined with a `plan_tier` filter returns 400 as redundant. |
| **Query** | ↓ |
| **Performance** | ≤ 12 × 4 rows. Target < 30 ms. |

Response:
```
{latest: {month, is_complete, mrr_usd, paying_workspaces, arpa_usd, billed_seats, mrr_growth_rate},
 series: [{month, is_complete, mrr_usd, paying_workspaces, arpa_usd, billed_seats,
           by_plan?: {tier: {mrr_usd, paying_workspaces, share}},
           movement: {new_usd, expansion_usd, contraction_usd, churned_usd, reactivation_usd, net_new_usd}}]}
```

```sql
SELECT month_start, plan_tier,
       SUM(mrr_usd) mrr_usd, SUM(paying_workspaces) paying, SUM(billed_seats) seats,
       SUM(new_mrr_usd) new_usd, SUM(expansion_mrr_usd) exp_usd, SUM(contraction_mrr_usd) con_usd,
       SUM(churned_mrr_usd) churn_usd, SUM(reactivation_mrr_usd) react_usd
FROM gold.fct_revenue_monthly
WHERE month_start BETWEEN :start_month AND :end_month
  AND (CAST(:plan_tier AS TEXT) IS NULL OR plan_tier = :plan_tier)
GROUP BY ROLLUP (month_start, plan_tier)
ORDER BY month_start, plan_tier NULLS FIRST;
```
`arpa_usd = mrr_usd / paying_workspaces`. This is the semantic metric `revenue_per_workspace`, with a null denominator giving null.

### 5.9 `GET /api/feature-adoption`

| | |
|---|---|
| **Purpose** | Adoption curves by days since signup, adoption at days 7, 30 and 90, and monthly active usage per feature, including the AI-adoption trend. |
| **Source** | `gold.fct_feature_adoption`, `gold.fct_feature_usage_monthly`, `gold.fct_activity_monthly` |
| **Parameters** | `features` (repeatable; each must be in `analytics.features`; default all), `max_day` (0–90, default 90), `start_month`, `end_month` |
| **Response** | See below |
| **Validation** | Unknown feature → 400 listing the valid ones. At most 10 features. |
| **Query** | ↓ |
| **Performance** | ≤ 8 × 91 + 12 × 8 rows. Target < 40 ms. |

Response:
```
{curves: [{feature, is_ai, points: [{day, adoption_rate}]}],
 adoption_at: [{feature, day_7_rate, day_30_rate, day_90_rate}],
 monthly: [{month, is_complete, active_workspaces, ai_active_workspaces, ai_adoption_rate,
            by_feature: {feature: {active_workspaces, workspace_share}}}]}
```
`meta.caveats` includes the right-censoring note (§0.3 #4).

```sql
SELECT feature_name, days_since_signup, cumulative_adoption_pct / 100.0 AS adoption_rate
FROM gold.fct_feature_adoption
WHERE feature_name = ANY(:features) AND days_since_signup <= :max_day
ORDER BY 1, 2;
```

### 5.10 `GET /api/experiments`

| | |
|---|---|
| **Purpose** | The experiment portfolio: every registered experiment with status and headline result. |
| **Source** | `experimentation.experiments.EXPERIMENTS` (registry) left-joined to `analytics.experiment_results` |
| **Parameters** | `decision` (optional: `SHIP \| CONTINUE \| HOLD \| REVERT`), `status` (optional: `running \| completed \| not_evaluated`) |
| **Response** | See below |
| **Validation** | Enum values only |
| **Query** | ↓ |
| **Performance** | One small query. Target < 20 ms. |

Response:
```
{experiments: [{experiment_id, description, kind ("ab"|"aa"), eligibility, start, end, traffic_share,
                status, decision_code, decision_text,
                primary: {metric: "activated_14d", control_rate, treatment_rate, relative_lift, p_value},
                srm: {p_value, detected}, sample: {control, treatment}}]}
```
`status` is `completed` when `end ≤ data_end`, `running` otherwise, and `not_evaluated` with no results row.

```sql
SELECT experiment_id, decision, control_users, treatment_users, control_rate, treatment_rate,
       relative_lift, p_value, srm_p_value, srm_detected
FROM analytics.experiment_results;
```
**Small changes to existing code:**
- `Experiment` gets `kind` (`'ab' | 'aa'`) and `hypothesis` fields, so the registry stays the single source and the A/A flag isn't hard-coded in the API.
- `evaluate_user_metrics` adds `decision_code` to the result. Until rows are re-persisted, the API parses `decision.split(' - ')[0]`.

### 5.11 `GET /api/experiments/{experiment_id}`

| | |
|---|---|
| **Purpose** | The full readout for one experiment: SRM, primary metric (frequentist and Bayesian), guardrails, sample sizes, decision, cumulative activation curve by arm and caveats. |
| **Source** | `analytics.experiment_results.result_json`, `gold.fct_experiment_activation_curve`, registry |
| **Parameters** | Path `experiment_id`: regex `^[a-z0-9_]{1,64}$`, then a registry lookup |
| **Response** | See below |
| **Validation** | Bad format → 422. Not in the registry → 404 problem with the known IDs listed. Registered but not evaluated → 200 with `evaluation: null`, `status: "not_evaluated"`. |
| **Query** | ↓ |
| **Performance** | Two point/range queries. Target < 30 ms. |

Response:
```
{experiment: {...registry fields...}, status,
 evaluation: {srm: {control_count, treatment_count, actual_ratio, p_value, detected},
              primary: {control_rate, treatment_rate, absolute_diff, relative_lift, ci_lower, ci_upper, p_value, significant},
              bayesian: {prob_treatment_better, expected_lift, expected_loss},
              guardrails: [{metric: "avg_session_minutes_14d"|"revenue_60d", control_mean, treatment_mean,
                            relative_lift, p_value, significant, failed}],
              sample_sizes: {...}, decision_code, decision_text},
 activation_curve: [{variant, points: [{day, activation_rate, users_in_window}]}]}
```
**Caveats:**
- The decision rule has no multiple-testing or peeking correction.
- For `kind = "aa"`, a SHIP is a false positive by construction (~5% of runs).

`result_json` keys come from `experimentation/stat_tests.py` and `bayesian_ab.py` and are mapped to typed schemas. A missing key fails loudly in a test, never silently in production.

```sql
SELECT result_json FROM analytics.experiment_results WHERE experiment_id = :id;
SELECT variant, day_since_signup, users_in_window, activated_cumulative
FROM gold.fct_experiment_activation_curve WHERE experiment_id = :id ORDER BY 1, 2;
```

### 5.12 `GET /api/nps`

| | |
|---|---|
| **Purpose** | NPS score with uncertainty, response mix, trend and plan breakdown. |
| **Source** | `gold.fct_nps_daily` |
| **Parameters** | `start`, `end` (default: the 90 days ending `data_end`), `plan_tier`, `granularity` (default `month`), `min_responses` (10–1000, default 30) |
| **Response** | `{summary: {nps, margin_of_error_95, responses, promoter_share, passive_share, detractor_share}, by_plan: [{plan_tier, nps, responses, ...}], series: Series[{responses, nps, margin_of_error_95}]}` |
| **Validation** | Shared rules. Any group with `responses < min_responses` gets `nps: null` and `suppressed_reason: "insufficient_responses"`. |
| **Query** | ↓ |
| **Performance** | ≤ 365 × 4 rows. Target < 30 ms. |

**Formulas** (`api/metrics.py`):
- NPS = `100·(P − D)/N`, where P, D and N are promoter, detractor and response counts.
- Margin of error (95%) = `1.96·100·sqrt(((P + D)/N − ((P − D)/N)²)/N)`, the variance of the per-response {+1, 0, −1} score.

```sql
SELECT DATE_TRUNC(:grain, response_date)::date AS period_start, plan_tier,
       SUM(responses) n, SUM(promoters) p, SUM(passives) pa, SUM(detractors) d
FROM gold.fct_nps_daily
WHERE response_date BETWEEN :start AND :end
  AND (CAST(:plan_tier AS TEXT) IS NULL OR plan_tier = :plan_tier)
GROUP BY GROUPING SETS ((1), (2), ())
```

### 5.13 `GET /api/support`

| | |
|---|---|
| **Purpose** | Support load and AI voice-agent performance. |
| **Source** | `gold.fct_support_daily`, `gold.fct_agent_performance_daily` |
| **Parameters** | `start`, `end` (default: the 90 days ending `data_end`), `plan_tier`, `call_type` (validated against `SELECT DISTINCT call_type`, cached), `granularity` (default `week`) |
| **Response** | See below |
| **Validation** | Shared rules. An unknown `call_type` → 400 listing the valid ones. |
| **Query** | ↓ |
| **Performance** | ≤ 365 × 4 × call types. Target < 40 ms. |

Response:
```
{tickets: {created, resolved, resolved_to_created_ratio, tickets_per_1k_active_user_days},
 ai_agent: {calls, ai_resolution_rate, escalation_rate, human_handled_share, avg_csat, avg_handle_time_seconds},
 by_call_type: [{call_type, calls, ai_resolution_rate, escalation_rate, avg_csat}],
 series: Series[{tickets_created, tickets_resolved, calls, ai_resolution_rate, avg_csat}]}
```
**Caveats:**
- There is no ticket ID, so resolution time and backlog aren't available.
- `resolved_to_created_ratio` is a count ratio, not a per-ticket rate.

```sql
SELECT DATE_TRUNC(:grain, call_date)::date AS period_start,
       SUM(calls) calls, SUM(ai_resolved) ai, SUM(escalated) esc, SUM(human_handled) hum,
       SUM(csat_sum) cs, SUM(csat_count) cn, SUM(handle_time_seconds_sum) ht
FROM gold.fct_agent_performance_daily
WHERE call_date BETWEEN :start AND :end
  AND (CAST(:plan_tier AS TEXT) IS NULL OR plan_tier = :plan_tier)
  AND (CAST(:call_type AS TEXT) IS NULL OR call_type = :call_type)
GROUP BY 1 ORDER BY 1;
```
`avg_csat = cs / cn`, a correct weighted mean. Averages are never averaged.

### 5.14 `GET /api/customer-health` and `GET /api/customer-health/workspaces` (added: back the Health tab)

| | |
|---|---|
| **Purpose** | The tier distribution and score histogram, plus a filterable, paginated list of workspaces for outreach. |
| **Source** | `analytics.workspace_health_scores` (latest `snapshot_date`); tiers and weights from `health_scoring` |
| **Parameters** | Summary: `plan_tier`. List: `tier`, `plan_tier`, `sort` (`health_score \| mrr_usd \| seat_count`), `order` (`asc \| desc`, default `asc`), `limit`, `offset` |
| **Response** | See below |
| **Validation** | Enums; pagination caps |
| **Query** | ↓ |
| **Performance** | 1K rows at 10K users and 50K at 500K. A new index `(snapshot_date, health_score)` is added to `persist_scores`' DDL. The list uses LIMIT/OFFSET with an offset cap; a keyset cursor can follow if needed. Target < 50 ms. |

Response:
```
summary → {snapshot_date, workspaces, mean_score,
           tiers: [{tier, min, max, workspaces, share}], histogram: [{bin_start, bin_end, workspaces, tier}],
           by_plan: [...], weights: {...}}
list    → {total, items: [{workspace_id, workspace_name, plan_tier, health_score, risk_tier, seat_count,
                           active_users_30d, dau_over_seats_ratio, used_ai_feature_30d, nps_score,
                           mrr_usd, mrr_change_usd, support_tickets_last_30d}]}
```
**Caveat:** scores are percentile-relative and the tiers are calibrated on synthetic data (Phase 2).

```sql
SELECT width_bucket(health_score, 0, 100.0001, 10) AS bin, COUNT(*)
FROM analytics.workspace_health_scores
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM analytics.workspace_health_scores)
  AND (CAST(:plan_tier AS TEXT) IS NULL OR plan_tier = :plan_tier)
GROUP BY 1 ORDER BY 1;
```
The list's `ORDER BY` column comes from an enum → column allow-list, followed by `workspace_id` as a stable tiebreaker.

### 5.15 Endpoint summary

| Endpoint | Main source | Default window | Target p95 (uncached, 10K) |
|---|---|---|---|
| `/api/health`, `/api/health/ready` | none / catalog + ops | n/a | 5 ms / 50 ms |
| `/api/meta` | gold, ops, registry | n/a | 30 ms |
| `/api/overview` | 8 serving tables | latest | 150 ms |
| `/api/engagement` | `fct_daily_active_users` | 1 year | 30 ms |
| `/api/activation` | `fct_activation_daily`, `_milestone_days` | 90 d ending `data_end − 14` | 60 ms |
| `/api/retention`, `/api/cohorts` | `fct_retention_cohorts` + `cohort_engine` | as of `data_end` | 80 ms |
| `/api/revenue` | `fct_revenue_monthly` | 12 months | 30 ms |
| `/api/feature-adoption` | `fct_feature_adoption`, `_usage_monthly`, `fct_activity_monthly` | all / 12 months | 40 ms |
| `/api/experiments`, `/{id}` | `analytics.experiment_results`, curve | n/a | 20 / 30 ms |
| `/api/nps` | `fct_nps_daily` | 90 d | 30 ms |
| `/api/support` | `fct_support_daily`, `fct_agent_performance_daily` | 90 d | 40 ms |
| `/api/customer-health[/workspaces]` | `analytics.workspace_health_scores` | latest snapshot | 50 ms |

Cached responses: < 10 ms.

---

## 6. Database connection management and pooling

- **One engine per worker process**, created in the FastAPI `lifespan` and disposed on shutdown. No engine is created at import time, so tests can override it.
- **URL:** `pipeline.config.database_url()` is extended to `database_url(database=None, user=None, password=None)`. Existing callers are unchanged; the API passes `API_DB_USER` / `API_DB_PASSWORD`.
- **Pool:** `QueuePool` with these settings:

| Setting | Env | Default | Why |
|---|---|---|---|
| `pool_size` | `API_DB_POOL_SIZE` | 5 | Sized to the threadpool |
| `max_overflow` | `API_DB_MAX_OVERFLOW` | 5 | Bursts |
| `pool_timeout` | `API_DB_POOL_TIMEOUT_S` | 5 | Fail fast → 503 `pool_exhausted` |
| `pool_recycle` | `API_DB_POOL_RECYCLE_S` | 1800 | Avoid stale server-side connections |
| `pool_pre_ping` | n/a | true | Postgres container restarts |

- **Connection budget:** workers × (pool_size + max_overflow) = 2 × 10 = 20. That stays well under PostgreSQL's default 100, alongside dbt threads, Airflow (separate database, same server) and GE.
- **Session settings** via `connect_args={"options": ...}`:
  - `-c default_transaction_read_only=on`
  - `-c statement_timeout=${API_STATEMENT_TIMEOUT_MS:-5000}`
  - `-c lock_timeout=2000`
  - `-c idle_in_transaction_session_timeout=10000`
  - `-c application_name=connecthub-api`

  `lock_timeout` makes a request fail fast with 503 `warehouse_busy` instead of hanging while dbt swaps a table.
- **Dependency:** `get_conn()` yields `engine.connect()` inside `conn.begin()`. That gives one read-only transaction per request, so the multi-query overview reads a consistent snapshot within a table. It is always closed.
- **Threadpool:** Starlette's AnyIO limiter (default 40 threads) is set to `pool_size + max_overflow + 2` at startup, so excess requests queue in the event loop rather than holding threads blocked on the pool.
- **Consistency during pipeline runs:**
  - dbt table materializations swap in a transaction, and incremental and analytics writes are transactional, so each query sees either the old or the new table.
  - Cross-table consistency during a run isn't guaranteed. `/api/meta` exposes the data version, and responses are cached by it, so a dashboard sees one version per cache entry.

---

## 7. Configuration

`api/settings.py` uses pydantic-settings. Environment variables are read only through Settings, which validates at startup and fails fast with a clear message and no secret values. `.env.example` gains the `API_*` block with placeholders only.

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` | `127.0.0.1` / `5432` / `connecthub_analytics` | Shared with the pipeline. `postgres` inside compose. |
| `API_DB_USER` | `connecthub_api` | Read-only role |
| `API_DB_PASSWORD` | *(required, no default)* | Secret |
| `API_HOST` / `API_PORT` / `API_WORKERS` | `127.0.0.1` / `8000` / `1` | `0.0.0.0` and 2 workers in the container |
| `API_ENV` | `development` | `production` enforces: docs disabled unless explicitly enabled, no wildcard CORS, `API_AUTH_MODE != none`. |
| `API_CORS_ORIGINS` | `http://127.0.0.1:8000,http://localhost:8000` | Comma list. `*` and `null` are rejected. |
| `API_DOCS_ENABLED` | `true` (dev) | `/api/docs`, `/api/redoc`, `/api/openapi.json` |
| `API_AUTH_MODE` | `none` | `none \| api_key` |
| `API_KEYS` | *(empty)* | Comma list of secret keys, compared with `hmac.compare_digest` |
| `API_CACHE_TTL_S` / `API_DATA_VERSION_TTL_S` | `300` / `30` | §8.2 |
| `API_STATEMENT_TIMEOUT_MS` | `5000` | Per session |
| `API_DB_POOL_*` | see §6 | |
| `API_LOG_LEVEL` | `INFO` | |
| `API_DASHBOARD_PATH` | *(unset)* | If set, serves `index.html` at `/` for same-origin dev (§12) |
| `API_DATASET_LABEL` | `synthetic` | Shown in `/api/meta` |

---

## 8. Cross-cutting behavior

### 8.1 Error handling (RFC 9457 `application/problem+json`)

```json
{"type": "https://connecthub.dev/problems/invalid-parameter", "title": "Invalid parameter",
 "status": 400, "detail": "start (2026-03-01) is after the last available date (2025-12-31)",
 "instance": "/api/nps", "request_id": "…", "errors": [{"loc": ["query", "start"], "msg": "…"}]}
```

| Condition | Status | `type` slug |
|---|---|---|
| Pydantic or parameter validation; unknown parameter | 422 / 400 | `validation-error` / `invalid-parameter` |
| Date range outside the data window; `start > end`; span too long | 400 | `invalid-range` |
| Unknown experiment | 404 | `experiment-not-found` |
| Unknown route | 404 | `not-found` |
| Method other than GET/HEAD/OPTIONS | 405 | `method-not-allowed` |
| Missing or invalid API key (when enabled) | 401 | `unauthorized` |
| Relation missing (`UndefinedTable`), meaning the warehouse is not built | 503 | `data-not-ready` |
| `OperationalError` connecting, or pool timeout | 503 + `Retry-After: 5` | `database-unavailable` / `pool-exhausted` |
| `QueryCanceled` (statement timeout) / `LockNotAvailable` | 504 / 503 | `query-timeout` / `warehouse-busy` |
| Anything else | 500 | `internal-error` |

A 500 never includes SQL, stack traces or DSNs. The full exception is logged with `request_id`, passed through `redact()`.

### 8.2 Caching and HTTP semantics

- **Data version:** the `run_id` of the latest `validate_analytics` step with `status = 'success'` in `ops.pipeline_runs`. That is the end of a fully validated run, whether local or Airflow. It is cached for `API_DATA_VERSION_TTL_S` (30 s).
- **Response cache:** an in-process TTL dict, limited to about 512 entries with LRU eviction and no new dependency. The key is `(route, normalized params, data_version)`, and the TTL is `API_CACHE_TTL_S`. A new successful run invalidates naturally.
- **Partial runs:** a run with only some steps (`--steps dbt`) does not bump the version. Staleness is then bounded by the TTL, as documented.
- **HTTP headers:** `ETag = sha256(data_version + route + params)` and `Cache-Control: private, max-age=60`. `If-None-Match` gets 304.
- **Other:** `GZipMiddleware(minimum_size=1024)`. HEAD is supported.
- **Readiness** responses are never cached.

### 8.3 Request validation

- **Typed parameters:** Pydantic query-parameter models (`extra="forbid"`), enums for every categorical value, and bounded ints, dates and string lengths.
- **Data-dependent validation** happens in a dependency that reads the cached `/api/meta` values: the date window, valid call types and the experiment registry.
- **Path parameters** are regex-validated before any lookup.
- **No user string ever reaches SQL text.** Values are bound; identifiers (grain, sort column) are mapped from enums to an allow-list of literals.

### 8.4 Structured logging

- **One JSON line per request:**
  ```
  {ts, level, event: "request", request_id, method, route (template, e.g. /api/experiments/{experiment_id}),
   status, duration_ms, db_ms, db_queries, cache: "hit"|"miss", data_version, client}
  ```
  It reuses `pipeline.log.JsonFormatter` and `redact()`.
- **Query strings** are logged in full; none carry secrets, since API keys go in headers and headers are never logged.
- **Uvicorn's access log** is disabled in favor of this; its error log stays JSON.
- **`X-Request-ID`:** accepted if it matches `^[A-Za-z0-9-]{8,64}$`, otherwise generated. It is echoed in the response and in every problem body.
- **Startup line:** logs the settings with secrets masked, the pool configuration and the data version.

### 8.5 API documentation

- FastAPI OpenAPI 3.1 at `/api/openapi.json`, plus Swagger at `/api/docs` and ReDoc at `/api/redoc`. These are toggled by `API_DOCS_ENABLED`.
- There is one tag per domain. Every response model and field has a description that cites `docs/metric-definitions.md`.
- Problem responses are declared per route (`responses={400: Problem, 404: Problem, 503: Problem}`).
- **No numeric examples are invented.** Examples are either structural (types only) or captured from a real validation run and labeled with its `data_version`.
- **New docs:**
  - `docs/api.md`: overview, conventions, auth, errors, caching and an endpoint table.
  - `docs/openapi.json`: a committed snapshot; a test fails if the generated spec drifts without the snapshot being updated.
  - `docs/metric-definitions.md`: gains the new metrics (NPS margin of error, tickets per 1K active user-days, AI resolution rate, ARPA, strict funnel).

### 8.6 Security

| Area | Measure |
|---|---|
| Database privilege | Dedicated role `connecthub_api`: LOGIN, `default_transaction_read_only = on`, `statement_timeout = 5s`, `CONNECT` on the database, `USAGE` on `gold` / `analytics` / `ops`, `SELECT` on their tables (only `ops.pipeline_runs` in `ops`), and nothing on bronze, staging, intermediate or experiments. `ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA gold, analytics GRANT SELECT ON TABLES` plus dbt `+grants` keep access after rebuilds. |
| Provisioning | `python -m api.provision` runs with **owner** credentials and is idempotent. It creates the schemas if they're missing, the role if missing, and sets the password with `psycopg2.sql` quoting, the role settings and the grants. It runs from the `api-init` one-shot compose service or `make api-role`. The `api` service never receives owner credentials. |
| Surface | GET/HEAD/OPTIONS only. No raw-SQL endpoint. No pass-through of table or column names. |
| Injection | Bound parameters everywhere; allow-listed identifiers; tests send injection strings to every parameter. |
| Auth | `API_AUTH_MODE=api_key` checks `X-API-Key` with a constant-time compare. It is required when `API_ENV=production`. The default for local use is `none`, bound to 127.0.0.1. |
| Network | Compose publishes on `127.0.0.1:${API_PORT}` only, the same rule as Postgres and Airflow. |
| CORS | `CORSMiddleware`: explicit origins only, `allow_methods=["GET","HEAD","OPTIONS"]`, `allow_credentials=False`, `allow_headers=["X-API-Key","X-Request-ID","If-None-Match"]`, `expose_headers=["ETag","X-Request-ID"]`. The `null` origin (file://) is not allowed; the dashboard is served over HTTP instead (§12). |
| Headers | `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and a `Content-Security-Policy` for JSON routes (`default-src 'none'`). The docs routes get a CSP that allows the Swagger CDN only when docs are enabled. |
| Secrets | Only from the environment and `.env` (git-ignored). Redacted in logs. Never in error bodies. The secret scan from Phase 3 is repeated over new files. |
| Limits | Max query-string length of 2 KB (middleware); the pagination and range caps above. Rate limiting is left to a reverse proxy and documented, not built. |
| Dependencies | Pinned in `requirements/api.txt` and the constraints file. The image runs as a non-root user. |

---

## 9. Data-quality additions

**dbt:** the tests listed in §3.

**GE gold stage** (`quality/expectations.py`):
- Grain uniqueness for the new models.
- Non-negative counts and money.
- Funnel monotonicity.
- NPS and agent reconciliation (as query assets returning 0 rows).
- `csat_sum / csat_count` in [1, 5].
- Rates derived from the serving tables in [0, 1].

These run in the existing `validate_gold` step, so a bad serving table stops the pipeline before the API can serve it.

---

## 10. Docker integration

**`docker/api/Dockerfile`:**
- `python:3.11-slim-bookworm` (bookworm pinned, per the Phase 3 trixie lesson).
- Installs `requirements/api.txt` with `-c requirements/constraints-py311.txt`.
- Copies `api/`, `analytics/`, `experimentation/` and `pipeline/` (for config and log).
- Non-root `app` user. `PYTHONDONTWRITEBYTECODE=1`.
- `CMD ["python", "-m", "api"]`.
- `HEALTHCHECK` with `python -c "urllib.request.urlopen('http://127.0.0.1:8000/api/health')"`, because slim images have no curl.

**Matplotlib and seaborn:**
- `analytics/cohort_engine.py` imports matplotlib and seaborn at module top.
- Phase 4 moves the pure functions (`retention_table`, `as_of`, `summarize`, `load_retention`) into `analytics/retention.py`. `cohort_engine` re-exports them, so the CLI, tests and the Airflow DAG are unchanged.
- The API image then needs no plotting libraries.

**`requirements/api.txt`:**
- **New:** `fastapi`, `uvicorn`, `pydantic-settings`.
- **Already present:** `SQLAlchemy==2.0.25`, `psycopg2-binary==2.9.9`, `pandas==2.2.0`, `numpy==1.26.3`.
- **Pinning:** pydantic is already pinned at 2.13.5 and httpx at 0.28.1 by the constraints file. The FastAPI and Starlette versions are chosen to be compatible with pydantic 2.13.5, added to the constraints, and verified with `pip check` in the dev venv, which shares an environment with Great Expectations 0.18.8.
- **Fallback:** if they conflict, the API gets its own environment, as Airflow did.
- **Dev venv:** `requirements.txt` adds `-r requirements/api.txt`.

**`docker-compose.yml`:**
```yaml
api-init:            # one-shot: python -m api.provision (owner creds; idempotent)
  build: {context: ., dockerfile: docker/api/Dockerfile}
  image: connecthub-api:local
  command: ["python", "-m", "api.provision"]
  environment: {<<: *pipeline-env, API_DB_USER: ..., API_DB_PASSWORD: ${API_DB_PASSWORD:?...}}
  depends_on: {postgres: {condition: service_healthy}}
  restart: "no"
api:
  image: connecthub-api:local
  environment: {POSTGRES_HOST: postgres, POSTGRES_DB: ..., API_DB_USER: ..., API_DB_PASSWORD: ...,
                API_HOST: 0.0.0.0, API_WORKERS: "2", API_CORS_ORIGINS: ...}
  ports: ["127.0.0.1:${API_PORT:-8000}:8000"]
  depends_on: {api-init: {condition: service_completed_successfully}}
  healthcheck: {test: [CMD, python, -c, "...urlopen('http://127.0.0.1:8000/api/health')"], interval: 30s}
  restart: unless-stopped
```
`docker compose up -d postgres api-init api` starts the API without Airflow. `api` does **not** depend on readiness: it starts and reports `not_ready` until the pipeline has built the warehouse.

**Makefile:** `api` (local uvicorn with reload), `api-role`, `api-up`, `api-test`, `api-bench`, `openapi` (refresh the snapshot).

**Memory:** about 150–200 MB per worker with pandas; 2 workers fit the ~3.8 GB Docker VM alongside Postgres.

---

## 11. Tests

| File | Kind | What it covers |
|---|---|---|
| `tests/api/test_params.py` | unit | Date-range rules (clamp, outside window, `start > end`, span cap), enums, unknown parameters → 400, pagination caps, path regex |
| `tests/api/test_metrics.py` | unit | Every formula in `api/metrics.py` on hand-built inputs: NPS and margin of error, strict funnel, ARPA, weighted CSAT, null on zero denominator, NPS suppression |
| `tests/api/test_errors.py` | unit | Each exception class → status, problem body, no SQL or DSN in the body, `request_id` present, `Retry-After` on 503 |
| `tests/api/test_middleware.py` | unit | Request ID propagation, security headers, CORS allow and deny (incl. `null` origin), GZip, ETag and 304, API-key mode |
| `tests/api/test_settings.py` | unit | Missing password fails startup; production-mode rules; wildcard CORS rejected; secrets masked in the startup log |
| `tests/api/test_openapi.py` | unit | Generated spec equals `docs/openapi.json`; every route has tags, a response model and problem responses |
| `tests/api/test_endpoints_integration.py` | integration | Every endpoint against a real warehouse: status, schema validity, `meta` populated, defaults anchored at `data_end` |
| `tests/api/test_parity.py` | integration | **The "no fake numbers" guarantee.** Each served value equals an independent computation on the same database (see the list after this table). |
| `tests/api/test_readonly_role.py` | integration | As `connecthub_api`: SELECT works; INSERT, CREATE and access to bronze, staging and intermediate are denied; statement timeout applies; grants survive a `dbt build` |
| `tests/api/test_injection.py` | integration | Injection payloads in every string parameter → 4xx, no 500, data unchanged |
| `tests/api/test_data_sensitivity.py` | integration, slow | The API pointed at two databases built with different seeds returns different overview values: catches anything hard-coded |
| `tests/test_dag.py`, e2e | existing | Still pass, with new models included in the fingerprints |

`tests/api/test_parity.py` checks these values against independent computations:
- **Overview DAU:** last row of `gold.fct_daily_active_users`.
- **MRR:** `SUM(gold.fct_workspace_mrr.mrr_usd)` for the month.
- **Week-4 retention:** `python -m analytics.cohort_engine --json`.
- **Activation rate:** direct SQL over `intermediate.int_activation_funnel`, run as the owner role.
- **NPS:** direct SQL over `staging.stg_nps_responses`.
- **Tickets:** `COUNT(*)` of `stg_events` support events.
- **AI resolution:** `fct_agent_evaluations`.
- **Experiment detail:** equals `evaluate.evaluate_experiment()` re-run live.
- **Health tiers:** `health_scoring.tier_counts()`.

**Integration fixture:**
- A session fixture builds a dedicated database `<db>_api_test` once by running `python -m pipeline run --users 2000 --database <db>_api_test`, reusing the e2e machinery (about 1 minute).
- It then provisions the API role there and creates the app with an engine pointed at it.
- Tests are marked `integration` and skip without `POSTGRES_PASSWORD`, like the existing ones. `make test-fast` stays DB-free.

**HTTP client:** `fastapi.testclient.TestClient`, using httpx, which is already installed.

**Not in pytest, done during validation (§14):**
- Docker image build and container healthcheck.
- `scripts/api_benchmark.py`: p50 and p95 per endpoint, cached and uncached, at 10K users and 100K if the host allows (Phase 3 found it doesn't).
- `EXPLAIN (ANALYZE)` for every repository query.

---

## 12. How `index.html` will consume the API (Phase 5; not changed now)

### 12.1 Approach

- **Keep the single-file design and Chart.js.**
- **Add a small data layer inside the page:**
  - `const API_BASE = window.CONNECTHUB_API_BASE ?? new URLSearchParams(location.search).get('api') ?? location.origin;`
  - `async function api(path, params)` → `fetch` with `If-None-Match`, parses the problem+json shape on error.
- **Load lazily:** `/api/meta` and `/api/overview` on load; other tabs on first activation. Each panel shows loading, empty and error states (for example "Warehouse not ready: run the pipeline", using the 503 `type`).
- **Serving:**
  - **Same origin in dev:** `API_DASHBOARD_PATH=index.html` makes the API serve the page at `/`, so no CORS is needed.
  - **GitHub Pages** (or any other host) uses `?api=` plus an entry in `API_CORS_ORIGINS`.
  - Opening `index.html` from `file://` is not supported, since the `null` origin is rejected.

### 12.2 Panel → endpoint mapping

| Tab / panel (current `index.html` element) | Endpoint | Fields |
|---|---|---|
| Header "Live" pill + "Oct 2025 — Sep 2026" | `/api/meta` | `data_start`, `data_end`, `last_successful_run.started_at` → "Data as of …" (the "Live" label is removed) |
| Overview KPI row (6 cards) | `/api/overview` | `kpis.mrr_usd`, `kpis.dau`, `kpis.week4_retention_rate`, `kpis.ai_feature_adoption_rate`, `kpis.activation_rate_14d`, `kpis.nps` (value + `change_*` + `period` tooltip) |
| `chart-dau` | `/api/engagement?granularity=month` | `series.points[].avg_dau` (and DAU/MAU) |
| `chart-adoption` | `/api/feature-adoption` | `curves[]` |
| `chart-revenue` (by plan) | `/api/revenue?group_by=plan_tier` | `series[].by_plan` |
| `chart-agent` | `/api/support?granularity=month` | `series[].ai_resolution_rate` |
| `heatmap-container` | `/api/cohorts` | `cohorts[].cells[]` (null → "—") |
| `chart-feat-retention` | `/api/retention` → `by_week1_features` | Only if optional model 10 ships; otherwise the panel is hidden |
| `funnel-container` | `/api/activation` | `funnel[]` |
| `chart-time-activation` | `/api/activation` | `time_to_milestone[].median_days` |
| Experiments tab (cards, verdict) | `/api/experiments` → `/api/experiments/{id}` | `evaluation.*`, `decision_code` → verdict class; an experiment selector replaces the fixed "Onboarding V2" card |
| `chart-exp-time` | `/api/experiments/{id}` | `activation_curve[]` |
| Health KPI row | `/api/customer-health` | `tiers[]` |
| `chart-health-dist` | `/api/customer-health` | `histogram[]` (also fixes the scriptable-color bug the audit found: use a per-bar color array) |
| `health-table-body` | `/api/customer-health/workspaces?limit=10` | `items[]`; rendered with `textContent` / escaped (workspace names are data) |
| New: NPS and Support panels | `/api/nps`, `/api/support` | Summary + series |
| "Key Insight" boxes | none | Removed. The hard-coded narratives contradict the data; data-driven insights belong to the AI-analyst phase. |
| AI Analyst tab | none in Phase 4 | Shows "not available yet" instead of the canned fallback until a grounded server-side analyst exists |

### 12.3 Literals that disappear

Every KPI, series, heatmap cell, funnel stage, experiment number, tier count, workspace row, `PROJECT_CONTEXT` and `getFallbackResponse()`.

---

## 13. Changes to existing files (planned)

| File | Change |
|---|---|
| `pipeline/config.py` | `database_url(database=None, user=None, password=None)`; existing behavior unchanged |
| `analytics/cohort_engine.py` → new `analytics/retention.py` | Move the pure retention functions (no plotting imports); `summarize(..., weeks=SUMMARY_WEEKS)`; re-export from `cohort_engine` |
| `analytics/health_scoring.py` | Add `CREATE INDEX IF NOT EXISTS … (snapshot_date, health_score)` to `persist_scores` |
| `experimentation/experiments.py` | `kind`, `hypothesis` fields |
| `experimentation/evaluate.py` | Add `decision_code` to the result (text unchanged) |
| `dbt_project/models/gold/*` | Models 1–9 (10 optional); `schema.yml` tests; `dbt_project.yml` grants |
| `quality/expectations.py` | Gold contracts for the new models |
| `docker-compose.yml`, `.env.example`, `Makefile`, `requirements*.txt`, constraints | §7, §10 |
| `README.md`, `docs/architecture.md`, `docs/metric-definitions.md` | API section; "serving API" moves out of "Not implemented"; new metric definitions |
| `index.html` | **No change in Phase 4** |

---

## 14. Work order and validation

Each step ends green (`pytest`, `ruff`, `dbt build`), and each is a commit on `phase-4`.

0. **Baseline.**
   - Start Docker and run `python -m pipeline run --users 10000`.
   - Confirm every §0.1 relation and column via `information_schema`; record row counts.
1. **dbt serving models 1–9.** Includes tests, GE contracts and grants. Then `dbt build`, `verify-incremental`, and the e2e idempotency test (fingerprints still identical across reruns).
2. **Small refactors.** `retention.py`, `database_url` arguments, registry fields, `decision_code`, health index. The existing tests are unchanged and pass.
3. **API skeleton.** Settings, db/pool, errors, logging, middleware, `/api/health`, `/ready`, `/meta`; `api.provision` plus read-only role tests.
4. **Endpoints.** In this order: overview, engagement, activation, retention, cohorts, revenue, feature-adoption, experiments, experiments/{id}, nps, support, customer-health. Each comes with its repository, schema, parity test and docs entry.
5. **Caching and ETag; OpenAPI snapshot; `docs/api.md`.**
6. **Docker.** Image, `api-init`, compose service, healthcheck. Then `docker compose up -d postgres api-init api`, the pipeline run, and readiness → `ready`.
7. **Benchmarks and `EXPLAIN`.** Record p50 and p95 against §5.15.
8. **`PHASE_4_REPORT.md`.**

**Acceptance criteria:**
- Every endpoint in §5 returns real values, and its parity test passes.
- The data-sensitivity test shows that values change with the dataset.
- The API role can't write or read outside `gold`, `analytics` and `ops.pipeline_runs`, and its grants survive `dbt build`.
- `pytest` (unit + integration), `ruff check .` and `dbt build` (with new tests) are all green. The e2e test still passes, with identical fingerprints across reruns.
- The OpenAPI snapshot is committed; docs and metric definitions are updated.
- `docker compose up -d postgres api-init api` gives a healthy container, and readiness reaches `ready` after the pipeline runs.
- p95 targets in §5.15 are met at 10K users, or the misses are reported with measurements.
- The Phase 3 secret scan is clean over all new files; no `.env` value appears in the image, logs or tracked files.

---

## 15. Risks and open decisions

| Risk / decision | Mitigation / recommendation |
|---|---|
| FastAPI/Starlette vs. pinned pydantic 2.13.5 and GE 0.18.8 in one venv | Resolve in step 3 with `pip check`. If they conflict, use a separate API environment (Airflow precedent). |
| New gold models add build time (models 5 and 8 scan `stg_events`) | Measured in step 1. Make them incremental with the `stg_events` pattern if they're significant. |
| Right-censored `fct_feature_adoption` | Caveat in the response now. Optional fix: a denominator of users with ≥ D days observed (changes the model's numbers; would be reported). |
| Health score is relative; A/A experiment currently "ships" | Surfaced as caveats and the `kind: "aa"` flag, not hidden. |
| Reads during a pipeline run | Transactional swaps, `lock_timeout`, data-versioned cache (§6, §8.2) |
| Host limits (7.7 GB RAM; Docker VM shared) | 2 workers. Benchmarks at 10K; 100K only if memory allows (Phase 3 could not). |
| Auth model for a public deployment | Phase 4 ships `api_key` mode. User accounts or OAuth and rate limiting are out of scope until there's a deployment target. |
| **Open:** keep `/api/...` unversioned (as specified) or add `/api/v1` | Recommendation: keep `/api/...` now and introduce `/api/v2` only on a breaking change. The OpenAPI snapshot test makes any change visible. |
| **Open:** build optional model 10 (`by_week1_features`)? | Recommendation: yes if time allows, since it backs an existing dashboard panel with a real metric. Otherwise the panel is hidden in Phase 5. |
