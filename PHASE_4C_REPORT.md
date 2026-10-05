# Phase 4C Report: Product Analytics API Endpoints

**Date:** 2026-10-05 · **Branch:** `phase-4` (on top of Phase 4B `3fa11e3`)
**Scope:** `PHASE_4_PLAN.md` §14 step 4: the 13 business endpoints, built on the Phase 4A serving tables and the Phase 4B architecture (routers → services → repositories).

**Not done (by design):**
- `index.html` and the frontend
- the AI analyst
- push to GitHub
- plan step 5: response caching, ETag, the OpenAPI snapshot test

## Bottom line

| Check | Result |
|---|---|
| `pytest` (complete suite, incl. end-to-end) | **240 passed, 2 skipped**, 481 s (was 185 + 2). 55 new tests. The skips are the usual container-only tests (§8). |
| API tests (`tests/api`) | **118 passed**, including the new parity, sensitivity and state tests |
| `ruff check .` | All checks passed |
| `dbt build` | Not affected (no dbt changes in 4C); last result PASS=106 in Phase 4B |
| Parity | Every endpoint's key values equal **independent SQL**, mostly against upstream relations (§5) |
| Data sensitivity | Changing bronze data changes the API responses by **exactly** the expected amounts (§5.3) |
| Real HTTP smoke test (container) | **27/27** checks: 19 endpoint variants, 7 error cases, CORS |
| Docker | `docker compose config` valid; image rebuilt; container healthy |
| Read-only | Warehouse fingerprint **identical** before and after the whole endpoint test module; the role is unchanged |
| Secret scan | 0 hits in 50 changed or new files (this report included) and in the container logs |
| No mock, random or hard-coded analytics | Scan of `api/` finds only the Phase 4B request-ID generator (`uuid4`). Every value comes from the warehouse (§4). |

---

## 1. Endpoint inventory

The full reference, including every parameter, default, rule and response field, is in **`docs/api.md`**. Summary:

| Endpoint | Source tables | Parameters (all others → 400) | Default window (relative to `data_end`, never today) |
|---|---|---|---|
| `GET /api/overview` | `fct_revenue_monthly`, `fct_daily_active_users`, `fct_retention_cohorts`, `fct_activity_monthly`, `fct_activation_daily`, `fct_nps_daily`, `fct_agent_performance_daily`, `analytics.workspace_health_scores`, `analytics.experiment_results` | none | Fixed period per KPI (`docs/api.md` table) |
| `GET /api/engagement` | `gold.fct_daily_active_users` | `start`, `end`, `granularity` | 365 days ending `data_end` |
| `GET /api/activation` | `gold.fct_activation_daily`, `gold.fct_activation_milestone_days` | `start`, `end`, `plan_tier`, `granularity`, `include_incomplete` | 90 signup days ending `data_end − 14` |
| `GET /api/retention` | `gold.fct_retention_cohorts` via `analytics/retention.py` | `as_of`, `cohort_start`, `cohort_end` | as of `data_end` |
| `GET /api/cohorts` | same | `as_of`, `cohort_start`, `cohort_end`, `weeks` | latest 26 complete cohorts |
| `GET /api/revenue` | `gold.fct_revenue_monthly` | `start_month`, `end_month`, `plan_tier`, `group_by` | 12 months ending at the latest month |
| `GET /api/feature-adoption` | `gold.fct_feature_adoption`, `fct_feature_usage_monthly`, `fct_activity_monthly` | `features` (repeatable), `max_day`, `start_month`, `end_month` | all features; 12 months |
| `GET /api/experiments` | registry + `analytics.experiment_results` | `decision`, `status` | n/a |
| `GET /api/experiments/{id}` | `analytics.experiment_results`, `gold.fct_experiment_activation_curve` | path `^[a-z0-9_]{1,64}$` | n/a |
| `GET /api/nps` | `gold.fct_nps_daily` | `start`, `end`, `plan_tier`, `granularity`, `min_responses` | 90 days ending `data_end` |
| `GET /api/support` | `gold.fct_support_daily`, `gold.fct_agent_performance_daily` | `start`, `end`, `plan_tier`, `call_type`, `granularity` | 90 days ending `data_end` |
| `GET /api/customer-health` | `analytics.workspace_health_scores` (latest snapshot) | `plan_tier` | latest snapshot |
| `GET /api/customer-health/workspaces` | same | `tier`, `plan_tier`, `sort` (allow-list: `health_score`, `mrr_usd`, `seat_count`, `active_users_30d`), `order`, `limit` (1–100), `offset` (0–10000) | latest snapshot, 25 per page, by score ascending |

**Behavior shared by all endpoints:**

| Topic | Rule |
|---|---|
| **Date windows** | Explicit ranges must have `start ≤ end`, span at most 400 days or 36 months, and overlap the loaded data; otherwise 400 `invalid-range`. A partial overlap is clamped, with a caveat. A **default** window longer than the data, such as the 365-day engagement default over 364 days of data, is fitted silently, because the user requested nothing. |
| **Incomplete and latest periods** | Every series point carries `is_complete`, false when the range cuts the period or it extends past `data_end`. Monthly tables use `month_complete`, activation uses `window_14d_complete`, and retention cells count only once their week has ended. Overview KPIs use only complete periods; a comparison period that starts before `data_start` gives `null` plus a note. |
| **Zero denominators** | Always `null`, never 0 (`api/metrics.py`). |
| **Empty results** | Valid 200 responses: zero counts, null rates, empty lists, documented `suppressed_reason` / `effective_range: null` (§5.2). An entirely unbuilt or empty warehouse is a 503 `data-not-ready`. |
| **Pagination and sorting** | Only `/api/customer-health/workspaces`. `ORDER BY <allow-listed column> <ASC/DESC> NULLS LAST, workspace_id` gives stable pages; the response has `total`, `next_offset` (null on the last page) and `items: []` past the end. |

## 2. Metric definitions

All metric definitions are in `docs/metric-definitions.md` (Phase 4A section "Serving models") and `docs/api.md`. The API computes ratios **only** in `api/metrics.py`, always from additive sums:
- `ratio`, `per_thousand` and `change` give null on a zero denominator.
- NPS = 100·(P − D)/N.
- NPS margin of error (95%) = 1.96·100·√(((P + D)/N − ((P − D)/N)²)/N).

**Reused business logic, nothing re-implemented:**

| Logic | Where it lives | Endpoints |
|---|---|---|
| Retention completeness and pooling | `analytics/retention.py` (`as_of` and the new `pooled_curve`, which `summarize` now uses, with outputs unchanged) | retention, cohorts, overview |
| Health scores and tiers | persisted output of `analytics/health_scoring.py`; tier bounds and weights imported from it | customer-health |
| Experiment results | persisted `analytics.experiment_results`; guardrail and decision rules from the new `experimentation/decision.py` (moved out of `evaluate.py` so the API needs no scipy) | experiments |
| Feature list | `analytics/features.py` | feature-adoption |

**Phase 4A decisions, preserved:**
- **Activation:** signups with an incomplete 14-day window are excluded by default and counted in `excluded_incomplete_signups`, never counted as not activated.
- **Retention:** an ended week with no row counts as 0%; a week that hasn't ended is `null`.
- **Feature adoption:** both `cumulative_rate` (censored) and `observed_rate` (eligible users only) are returned, explicitly named.
- **Tickets:** counts only, and the "no ticket ID" caveat appears in every support response.

## 3. Response examples

These are real responses from the running container, dev dataset, `data_version` `phase4b-run-1`, trimmed:

```json
GET /api/overview  -> data.kpis.mrr_usd
{"value": 61870.0, "previous_value": 56860.0, "change_abs": 5010.0, "change_rel": 0.088111,
 "period": {"start": "2025-12-01", "end": "2025-12-31"},
 "previous_period": {"start": "2025-11-01", "end": "2025-11-30"},
 "unit": "usd", "definition": "MRR of the latest complete month vs the month before", "note": null}
meta: {"as_of": "2025-12-31", "data_start": "2025-01-02", "data_end": "2025-12-31",
       "data_version": "phase4b-run-1", "sources": ["gold.fct_revenue_monthly", ...]}

GET /api/activation  (default: complete windows only)
{"signups": 3616, "funnel": [{"stage": "signed_up", "users": 3616, "rate_of_signups": 1.0, "rate_of_previous": null},
 {"stage": "placed_first_call", "users": 2500, "rate_of_signups": 0.691372, "rate_of_previous": 0.691372}, ...]}
meta.effective_range: {"start": "2025-09-19", "end": "2025-12-17"}

GET /api/nps  -> data.summary
{"responses": 987, "promoters": 374, "passives": 322, "detractors": 291, "nps": 8.4,
 "margin_of_error_95": 5.1, "promoter_share": 0.378926, "passive_share": 0.326241,
 "detractor_share": 0.294833, "suppressed_reason": null}

GET /api/nps?start=2025-01-02&end=2025-01-20  (before the first response: empty, not an error)
{"responses": 0, ..., "nps": null, "margin_of_error_95": null, "promoter_share": null, ...,
 "suppressed_reason": "no_responses"}

GET /api/support  -> data.tickets / data.ai_agent
{"created": 1834, "resolved": 1470, "resolved_to_created_ratio": 0.801527, "active_user_days": 53054,
 "tickets_per_1k_active_user_days": 34.569}
{"calls": 7325, "ai_resolved": 4720, "escalated": 1540, "human_handled": 1065, "ai_resolution_rate": 0.644369,
 "escalation_rate": 0.210239, "human_handled_share": 0.145392, "avg_csat": 4.017, "avg_handle_time_seconds": 168.381}

GET /api/experiments/exp_onboarding_v2  -> status, decision, primary
{"status": "completed", "decision_code": "SHIP", "primary": {"control_rate": 0.1867, "treatment_rate": 0.2538,
 "absolute_diff": 0.0671, "relative_lift": 0.3593, "ci_lower": 0.0505, "ci_upper": 0.0837, ...}}

GET /api/retention  -> data.headline
{"week_1": 0.75402, "week_4": 0.39431, "week_8": 0.26773, "week_12": 0.218811}

GET /api/customer-health/workspaces?limit=1
{"total": 1000, "limit": 1, "offset": 0, "next_offset": 1, "sort": "health_score", "order": "asc",
 "items": [{"workspace_name": "Workspace_22", "plan_tier": "Enterprise", "health_score": 10.6,
            "risk_tier": "Critical", "nps_score": null, "pct_ai_calls_automated": null, ...}]}
```

## 4. Correctness guarantees and how they're enforced

- **SQL only in repositories, values only as bound parameters.**
  - Optional filters are fixed fragments: `CAST(:x AS text) IS NULL OR col = :x`.
  - Granularity is bound: `DATE_TRUNC(CAST(:grain AS text), ...)`.
  - The workspace sort is a dict lookup to a fixed `ORDER BY` string. No user string ever reaches SQL text.
- **No raw-event scans per request:** every repository reads a serving table, a gold fact or `analytics.*` (§6.3 plans).
- **No mock, random or canned values:** the scan of `api/` found only `uuid4` (request IDs).
- **Constants are definitions or parameters, not metric values:**
  - default window lengths: 90 / 365 days, 12 months, 26 cohorts
  - the 14-day activation window and the 28-day DAU comparison
  - the NPS suppression threshold (30)
  - the histogram bin width (5)
- **Enums mirror their sources of truth:** a test asserts `PlanTier` equals `quality.expectations.PLAN_TIERS` and `RiskTier` equals `health_scoring.TIER_LABELS`.

## 5. Tests added (55 new; 118 API tests in total)

| File | Kind | Tests | Covers |
|---|---|---|---|
| `tests/api/test_api_params_metrics.py` | unit | 16 | Defaults anchored at `data_end`; one-sided ranges; clamping (and none for defaults); invalid ranges; month ranges; the day-granularity limit; period completeness; zero denominators; NPS and margin math; tier boundaries; enums vs sources of truth; decision helpers |
| `tests/api/test_api_endpoints.py` | integration (10K warehouse) | 32 | Parity per endpoint; date filters and boundaries; incomplete windows; empty results; zero denominators; pagination and sort allow-list; invalid and unsupported parameters; 6 injection payloads × 10 parameters + the path; response-schema validation; warehouse unchanged by all requests |
| `tests/api/test_api_endpoint_states.py` | unit + integration | 5 | All 13 endpoints with Postgres unreachable (503 + `Retry-After`); API key required on all; every route documented, tagged, GET-only; warehouse not built and tables without data (503 `data-not-ready`) |
| `tests/api/test_api_sensitivity.py` | integration (pipeline-built 1,500-user DB) | 2 | Exact response changes after data changes (§5.3); missing persisted evaluation → `not_evaluated`; emptied health table → 503 while the overview stays 200 |

**Phase 4B tests updated**, both kept at least as strict (diff reviewed):
- `test_unknown_route_is_a_404_problem` used `/api/overview` as its "unknown" route, which now exists, so it uses `/api/does-not-exist`.
- The OpenAPI test asserted exactly the 3 Phase 4B paths. It now asserts exactly the 16 implemented paths, **and** that every path is GET-only.

No test was deleted.

### 5.1 Parity: API vs independent SQL

| Endpoint | Compared against |
|---|---|
| overview | MRR from `fct_workspace_mrr` (latest complete month); DAU = `COUNT(DISTINCT user_id)` in `stg_events` on `data_end` and `data_end − 28`; week-4 retention, recomputed in SQL with the completeness rule at both dates; AI adoption from `int_feature_usage`; activation from `int_activation_funnel`; NPS from `stg_nps_responses`; AI resolution from `fct_agent_evaluations`; tier counts; decisions |
| engagement | DAU and active workspaces from `stg_events`; weekly mean DAU from per-day distinct counts |
| activation | Funnel counts per plan from `int_activation_funnel` + billed-plan join; medians and p75 = `percentile_disc` over per-user days; incomplete-window counts from `stg_users` |
| retention | Every pooled week (cohorts, users, active) from SQL implementing the completeness and zero-cell rules, as of `data_end` and as of 2025-06-30 |
| cohorts | Every cell vs `fct_retention_cohorts`, with zero fill and null for unfinished weeks |
| revenue | MRR, paying workspaces, seats and net change per month and plan from `fct_workspace_mrr`; movement net = MRR − previous; shares sum to 1 |
| feature adoption | Monthly AI adoption and per-feature workspaces from `int_feature_usage`; curve points vs `fct_feature_adoption` |
| experiments | Rates, p-values, samples, decision vs `analytics.experiment_results`; detail vs `result_json`; curve day 14 = evaluated rates |
| nps | Counts and score from `stg_nps_responses`; per-plan responses via billed-plan join |
| support | Tickets from `stg_events`; active user-days = Σ DAU from `fct_daily_active_users`; calls, resolution, CSAT and handle time from `fct_agent_evaluations` |
| customer health | Tier counts, mean, histogram total, plan filter; the first 50 workspaces by MRR equal SQL `ORDER BY` |

### 5.2 Edge cases verified

- **Activation:**
  - a range entirely after `data_end − 14` gives 0 signups, null rate, `effective_range: null`, empty series
  - the default end is exactly `data_end − 14`
  - `include_incomplete` adds exactly the excluded signups
- **NPS:** a window before the first response gives `no_responses` with null shares; `min_responses=1000` gives `insufficient_responses`.
- **Support:** a window before the first AI call gives 0 calls with null rates and CSAT.
- **Revenue:** Free plan gives null ARPA (0 paying workspaces). Growth is null when previous MRR is 0, or −1.0 when it comes from workspaces that downgraded to Free.
- **Experiments:** filters with no match give `experiments: []`; an unregistered ID gives 404 even on an empty warehouse (the registry is checked first).
- **Pagination:** an offset past the end gives `items: []`; the last page has `next_offset: null`.
- **Engagement:** early periods are flagged `mau_window_complete: false`; the last week is `is_complete: false`.

### 5.3 Data sensitivity: responses follow the data

On a separate warehouse built by the real pipeline (1,500 users), the test:
1. records the responses
2. inserts 20 detractor NPS responses, 50 tickets, 5 newly active users on `data_end`, and all 3 activation milestones for 25 non-activated control users
3. adds $1,000 to one workspace's latest MRR
4. rebuilds (`dbt run --full-refresh` + `pipeline step analytics`)
5. requires exact changes:
   - NPS responses and detractors +20, score down
   - tickets +50
   - latest MRR and overview MRR +1,000
   - engagement DAU and overview DAU +5
   - experiment `control_rate` = (activated + 25) / n, from the pre-change counts
   - curve day-14 activated +25
   - the workspace's tickets +50 and its NPS changed

It also proves the read-only role keeps reading after a full dbt rebuild (default privileges).

## 6. Performance (10K users only)

### 6.1 Latency

Measured over real HTTP against the container (2 uvicorn workers): 2 warm-ups, then 20 sequential requests per endpoint. Server-side times come from the API's own request log for the same requests.

| Endpoint (default parameters unless noted) | Server p50 | DB p50 (queries) | Client p50 / p95 |
|---|---|---|---|
| engagement | 5.3 ms | 1.6 ms (4) | 6.3 / 7.3 ms |
| customer-health/workspaces | 6.3 ms | 2.0 ms (5) | 5.8 / 6.6 ms |
| revenue | 5.9 ms | 1.4 ms (4) | 8.1 / 8.6 ms |
| experiments, experiments/{id} | 4.9–5.0 ms | 1.1–1.3 ms (3–4) | 50.0 / 50.2 ms |
| nps | 5.9 ms | 1.9 ms (5) | 50.0 / 60.2 ms |
| customer-health | 6.9 ms | 2.9 ms (7) | 50.0 / 50.2 ms |
| activation | 7.7 ms | 3.7 ms (6) | 50.1 / 60.3 ms |
| support | 9.0 ms | 4.4 ms (7) | 59.9 / 60.2 ms |
| cohorts | 12.9 ms | 1.3 ms (3) | 14.4 / 16.9 ms |
| feature-adoption (65 KB response) | 12.9 ms | 2.2 ms (6) | 14.7 / 16.9 ms |
| overview | 22.2 ms | 6.2 ms (16) | 70.0 / 79.3 ms |
| retention | 45.5 ms | 1.3 ms (3) | 90.0 / 99.9 ms |

**What these show:**
- **Server cost:** 5–23 ms for all endpoints except retention (45 ms, spent in pandas pooling, of which 1.3 ms is SQL). Every endpoint is within the plan's §5.15 targets.
- **Client times above server cost:** several client times sit at exactly 50.0 or 60.0 ms while their server time is ~5 ms. That is a fixed artifact of the host → container hop (Docker Desktop port forwarding on Windows, most likely interacting with TCP delayed ACK), not API work. Endpoints with the same server cost (engagement, revenue) show 6–8 ms on the client. I didn't optimize for it.
- **Retention:** the remaining real cost is pandas pooling (45 ms). It is acceptable at this scale and not optimized without a need.

### 6.2 Scale

Serving tables grow with days × plans, not users. The largest table read per request, `fct_activation_milestone_days`, has 11K rows; the workspace list grows with workspaces (1K here). **Nothing was measured above 10K users**, so no claim is made for 100K or 500K.

### 6.3 Query plans (`EXPLAIN ANALYZE`, median of 5)

| Query | Time | Plan |
|---|---|---|
| Engagement periods (365 days) | 0.33 ms | Seq Scan on `fct_daily_active_users` (364 rows) |
| Activation totals (90 days) | 0.15 ms | Seq Scan on `fct_activation_daily` |
| NPS periods (90 days, plan filter) | 0.11 ms | Seq Scan on `fct_nps_daily` |
| Agent by call type (90 days) | 0.59 ms | Seq Scan on `fct_agent_performance_daily` |
| Retention cells | 0.12 ms | Seq Scan on `fct_retention_cohorts` |
| Workspace page (score ascending, 25) | 0.13 ms | **Index Scan using `workspace_health_scores_score_idx`** (the Phase 4B index) |

Sequential scans on tables of at most about 11K rows are the right plan; no further indexes are warranted.

## 7. Security validation

| Check | Result |
|---|---|
| Read-only | Warehouse fingerprint (gold + analytics) identical before and after all endpoint tests. The role's read-only and no-write privileges are re-verified by the Phase 4B tests. GET-only surface (OpenAPI test). |
| Injection | 6 payloads (quote breaks, stacked `DROP`/`DELETE`, `pg_sleep`, URL-encoded) in 10 parameters plus the path: always 400 or 422 (or 404 for the path), never 500, no side effects |
| Bound parameters | All user values are bound; identifiers come only from allow-lists |
| Errors | Problem bodies without SQL or DSNs (Phase 4B tests); 503s carry `Retry-After` |
| Auth | All 13 endpoints return 401 without a key in `api_key` mode; health stays open |
| CORS and headers | Configured origin allowed, others denied; security headers on every response (smoke test and Phase 4B tests) |
| Secrets | 0 hits across 50 files and the container logs |

## 8. Skipped tests

| Test | Why |
|---|---|
| `tests/test_dag.py` | Airflow isn't in the dev venv; it runs in the Airflow container |
| `tests/test_spark_jobs.py` | pyspark isn't in the dev venv; it runs in the Spark container |

## 9. Deviations from the plan

1. **One router module** (`api/routers/analytics.py`) instead of a file per domain. The routes are thin and HTTP-only, and the services and repositories *are* per domain.
2. **Customer-health histogram:** 20 bins of 5 points instead of 10 bins of 10, so every bin falls in exactly one tier. The tier bounds 40/55/70 don't align with 10-point bins. Bins are right-closed, like `pd.cut` in `health_scoring.py`.
3. **`docs/api.md` written now** (the plan placed it in step 5), because you asked for each endpoint to be documented explicitly. The rest of step 5 (cache, ETag, OpenAPI snapshot) is not done.
4. **Two small refactors:**
   - `experimentation/decision.py`: `guardrail_failed` and `decision_code` moved out of `evaluate.py`, which imports scipy and can't run in the API image. `evaluate.py` re-imports them, so it behaves identically.
   - `analytics/retention.pooled_curve`: `summarize` now uses it. Its outputs are unchanged, verified by the CLI and the existing tests.

## 10. Known limitations

1. **No response cache** (plan step 5). Each request queries the warehouse; at 10K users that costs 5–45 ms server-side.
2. **Benchmarks at 10K only.** The client-side ~50 ms floor is a Docker-on-Windows networking artifact, not API cost.
3. **Plan tier differs by domain**, as documented:
   - activation, revenue, NPS and support use the plan billed in that month
   - customer health uses the workspace's current plan (as persisted by `health_scoring.py`)
   - per-plan revenue movements are relative to this month's plan
4. **The experiment list is registry-driven.** A persisted result for an experiment no longer in the registry isn't listed.
5. **The overview's NPS threshold is fixed at 30 responses.** `/api/nps` exposes it as `min_responses`.
6. **Carried over from Phase 4B:**
   - Starlette 1.x TestClient deprecation (filtered)
   - 2.5 GB of free disk on C:
   - stale Airflow image (needs `docker compose build`)
   - port 8000/8001 occupied on this machine (local `.env` uses 8002)

## 11. Files changed

**Added (40):**
- **API core:** `api/metrics.py`, `api/routers/analytics.py`, `api/services/context.py`.
- **Per domain:** `api/repositories/` (10: activation, adoption, customer_health, engagement, experiments, nps, overview, retention, revenue, support), `api/schemas/` (10, the same domains) and `api/services/` (10, the same domains).
- **Other:** `experimentation/decision.py`, `docs/api.md`, `PHASE_4C_REPORT.md`.
- **Tests:** `tests/api/test_api_params_metrics.py`, `test_api_endpoints.py`, `test_api_endpoint_states.py`, `test_api_sensitivity.py`.

**Modified (10):**

| File | Change |
|---|---|
| `api/main.py` | Router and tags |
| `api/params.py` | Range, month and period helpers; enums |
| `api/errors.py` | `experiment-not-found` |
| `api/schemas/common.py` | `Period`, `Kpi` |
| `analytics/retention.py` | `pooled_curve` |
| `experimentation/evaluate.py` | Imports from `decision.py` |
| `docker/api/Dockerfile` | Copies `decision.py` |
| `docs/architecture.md` | API section |
| `README.md` | API section and structure |
| `tests/api/test_api_http.py` | 2 expectations updated (§5) |

**Not touched:** `index.html`, the dbt project, the DAG, `.env.example`, the compose file.

## 12. Commit

See the git log for the commit hash (`Phase 4C: implement product analytics API endpoints`).
