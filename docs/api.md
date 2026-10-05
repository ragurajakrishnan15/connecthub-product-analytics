# Analytics API Reference

The read-only HTTP API over the analytics warehouse (`api/`). The design is in
`PHASE_4_PLAN.md`; metric definitions are in [metric-definitions.md](metric-definitions.md).
Interactive docs at `/api/docs` (Swagger) and `/api/redoc` when `API_DOCS_ENABLED`.

```bash
python -m api.provision        # once: the read-only role (owner credentials)
python -m api                  # 127.0.0.1:${API_PORT:-8000}
docker compose up -d postgres api-init api
```

## Conventions (all data endpoints)

| Topic | Rule |
|---|---|
| **Envelope** | `{"data": ..., "meta": {as_of, data_start, data_end, effective_range, filters, sources, data_version, generated_at, definitions, caveats}}` |
| **Sources** | Only the gold serving tables, `analytics.*` and `ops.pipeline_runs`; never raw events. Each response lists its relations in `meta.sources`. |
| **Dates** | ISO `YYYY-MM-DD`, inclusive ranges. Weeks start Monday. |
| **Default windows** | Relative to `data_end`, the last loaded event date (2025-12-31 for the dev dataset), **never today's date**. |
| **Explicit ranges** | `start <= end` and at most 400 days (36 months), otherwise 400 `invalid-range`. A range must overlap the loaded data (otherwise 400 `invalid-range`). A partial overlap is clamped, with a caveat. |
| **Granularity** | `day` / `week` / `month`; `day` only for ranges of up to 120 days. |
| **Periods** | Every series point has `period_start` / `period_end` (the part inside the range) and `is_complete`, which is false when the range cuts the period or it extends past `data_end`. Periods without data are returned with zero counts. |
| **Rates** | Fractions in [0, 1] (gold stores some as percent; the API converts them). Computed from additive sums, never averages of averages. |
| **Zero denominators** | `null`, never 0. |
| **Money** | USD, 2 decimals, fields ending `_usd`. |
| **Parameters** | Undeclared parameter: 400 `invalid-parameter`. Malformed value or enum: 422 `validation-error`. |
| **Errors** | RFC 9457 `application/problem+json`, with `type` `urn:connecthub:problem:<slug>` and a `request_id`. 503 `data-not-ready` until the pipeline has built and loaded the warehouse; 503 `database-unavailable` (with `Retry-After`); 504 `query-timeout`. |
| **Auth** | `X-API-Key` when `API_AUTH_MODE=api_key`. `/api/health*` are always open. |

## Endpoints

### `GET /api/overview`

Headline KPIs. **No parameters.**

**Sources:** `fct_revenue_monthly`, `fct_daily_active_users`, `fct_retention_cohorts`, `fct_activity_monthly`, `fct_activation_daily`, `fct_nps_daily`, `fct_agent_performance_daily`, `analytics.workspace_health_scores`, `analytics.experiment_results`.

| KPI | Current period | Previous period |
|---|---|---|
| `mrr_usd` | Latest **complete** month (`month_complete`) | Previous complete month |
| `dau` | `data_end` | `data_end − 28` (same weekday) |
| `week4_retention_rate` | Pooled as of `data_end` | As of `data_end − 28` |
| `ai_feature_adoption_rate` | Latest complete month: AI-active ÷ feature-active workspaces | Month before |
| `activation_rate_14d` | Signups in [`data_end − 43`, `data_end − 14`]: 30 days, all with complete windows | The 30 signup days before |
| `nps` | Last 90 days (null below 30 responses) | 90 days before |
| `ai_resolution_rate` | Last 30 days | 30 days before |

**KPI object:** `{value, previous_value, change_abs, change_rel, period, previous_period, unit, definition, note}`. A period that starts before `data_start` has value `null` and a `note`.

**Also returned:** `health` (tier counts and shares, latest snapshot) and `experiments` (registered, evaluated, counts by decision).

### `GET /api/engagement`

DAU / WAU / MAU trend. **Source:** `gold.fct_daily_active_users`.

**Parameters:**

| Parameter | Default |
|---|---|
| `start`, `end` | The 365 days ending `data_end` |
| `granularity` | `week` |

**`summary`:** values on the last day of the range: `dau`, `wau_7d`, `mau_28d`, `active_workspaces`, `stickiness_rate` (= DAU/MAU; null if MAU is 0), `mau_window_complete`.

**`points[]`:** `avg_dau`, `avg_active_workspaces`, `wau_7d_end`, `mau_28d_end` (values on the period's last day), `avg_stickiness_rate` (mean of daily DAU/MAU), `mau_window_complete` (false when the 28-day window starts before `data_start`, which makes MAU understated).

### `GET /api/activation`

14-day activation by **signup date**. **Sources:** `gold.fct_activation_daily`, `gold.fct_activation_milestone_days`.

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `start`, `end` | The 90 signup days ending `data_end − 14` (the last date with a complete window) |
| `plan_tier` | Optional; the plan billed in the signup month |
| `granularity` | `week` |
| `include_incomplete` | `false` |

**Incomplete windows:** a signup's window is complete when `signup_date + 14 <= data_end`. By default, signups after `data_end − 14` are **excluded and counted** in `excluded_incomplete_signups`, and `effective_range` ends at `data_end − 14`; they are never treated as not activated. With `include_incomplete=true` they are included, with a caveat. If no signup in the range has a complete window, the response has zero counts, null rates, `effective_range: null` and an empty series.

**Response fields:**
- **`funnel[]`** (strict): signed_up → placed_first_call → call_and_ai → activated_14d, each with `users`, `rate_of_signups` and `rate_of_previous`.
- **`milestones`:** independent rates per milestone.
- **`time_to_milestone[]`:** `median_days` / `p75_days` = the smallest day by which at least 50% / 75% of the users who reached the milestone within 14 days had reached it. Exact, from integer days.
- **`series[]`:** `signups`, `activated_14d`, `activation_rate_14d` per period.

### `GET /api/retention`

Pooled weekly retention. **Source:** `gold.fct_retention_cohorts`, through `analytics/retention.py` (`as_of`, `pooled_curve`).

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `as_of` | `data_end`; must be within the data |
| `cohort_start`, `cohort_end` | Optional; snapped to their Monday, with a caveat |

**Cohorts and pooling:**
- A cohort is the Monday-start week of each user's first product activity; there is no plan breakdown.
- Week N of a cohort counts only once it has ended by `as_of`.
- An ended week with no row in gold (nobody active) counts as 0; a week that hasn't ended is excluded.

**Response fields:**
- **`pooled_curve[]`** (weeks 0–12): `retention_rate`, `cohorts_included`, `users_included`, `active_users`. The rate is null when no cohort is eligible.
- **`headline`:** weeks 1, 4, 8 and 12.
- **`by_cohort_month[]`:** week-1 and week-4 rates per cohort month.

### `GET /api/cohorts`

Cohort × week matrix. **Source:** same as `/api/retention`.

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `as_of` | `data_end` |
| `cohort_start`, `cohort_end` | The latest 26 cohorts whose first week is complete; at most 53 per request (otherwise 400) |
| `weeks` | 12 (allowed 1–12) |

**Cells:** `{week, active_users, retention_rate}`. An ended week with no activity is **0**; a week that hasn't ended by `as_of` is **`null`**.

### `GET /api/revenue`

Monthly recurring revenue. **Source:** `gold.fct_revenue_monthly`.

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `start_month`, `end_month` (`YYYY-MM`) | The 12 months ending at the latest month |
| `plan_tier` | Optional |
| `group_by` | `plan_tier` (or `none`); combining `group_by=plan_tier` with a `plan_tier` filter is a 400 |

**Per month:**
- `mrr_usd`, `previous_mrr_usd`, `mrr_growth_rate` (= (MRR − previous) ÷ previous; null if previous is 0).
- `paying_workspaces`, `arpa_usd` (= MRR ÷ paying workspaces; null with none), `billed_seats`.
- `movement` (new, expansion, reactivation ≥ 0; contraction, churned ≤ 0; `net_new_usd`).
- `by_plan` (with `share_of_mrr`).
- `is_complete`: false while the month extends past `data_end`.

**Top level:** `latest` (the last month, possibly incomplete) and `latest_complete`.

### `GET /api/feature-adoption`

**Sources:** `gold.fct_feature_adoption`, `gold.fct_feature_usage_monthly`, `gold.fct_activity_monthly`.

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `features` | Repeatable; default all; at most 10; an unknown feature is a 400 listing the valid ones |
| `max_day` | 90 (allowed 0–90) |
| `start_month`, `end_month` | The 12 months ending at the latest month |

**`curves[].points[]`:** each point carries both adoption rates, explicitly named (Phase 4A decision):
- `cumulative_rate`: adopters by day D ÷ **all** users. Right-censored, so it understates later days.
- `observed_rate`: adopters by day D among users with at least D days of history ÷ those users. Null when none are eligible.
- `eligible_users`.

**Also returned:**
- **`adoption_at[]`:** days 7, 30 and 90.
- **`monthly[]`:** `ai_adoption_rate` (= AI-active ÷ feature-active workspaces, the semantic metric `ai_feature_adoption`), plus `workspace_share` per feature.

### `GET /api/experiments`

The experiment portfolio. **Sources:** the registry (`experimentation/experiments.py`) and `analytics.experiment_results`, i.e. the evaluations persisted by the pipeline and validated by Great Expectations. **Nothing is re-evaluated per request.**

**Parameters:**
- `decision`: `SHIP` / `CONTINUE` / `HOLD` / `REVERT`
- `status`: `running` / `completed` / `not_evaluated`

**`status`:** `not_evaluated` without a persisted result; `completed` when the experiment end ≤ `data_end`; otherwise `running` (interim results). Filters that match nothing return `experiments: []`.

### `GET /api/experiments/{experiment_id}`

`experiment_id` must match `^[a-z0-9_]{1,64}$` (otherwise 422). An unregistered ID is a 404 `experiment-not-found`, listing the known IDs. A registered experiment without a result returns `status: not_evaluated` and `evaluation: null`.

**`evaluation`:** `srm`, `primary` (rates, difference, 95% CI, z, p), `bayesian`, `guardrails[]` (`failed` = a significant decrease, per `experimentation/decision.py`), `sample_sizes`, `decision_code`, `decision_text`.

**`activation_curve[]`:** per arm, the cumulative activation rate for days 0–14 (complete windows only); day 14 equals the evaluated rate. The caveats state the lack of a multiple-testing correction and, for `kind: aa`, that a SHIP is a false positive by construction.

### `GET /api/nps`

**Source:** `gold.fct_nps_daily`.

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `start`, `end` | The 90 days ending `data_end` |
| `plan_tier` | Optional |
| `granularity` | `month` |
| `min_responses` | 30 (allowed 10–1000) |

**Values** (`summary`, `by_plan[]`, `series[]`):
- `nps` = 100 × (promoters − detractors) ÷ responses.
- `margin_of_error_95`, in NPS points.
- Promoter, passive and detractor shares.

**Suppression:** below `min_responses`, `nps` and the margin are null, with `suppressed_reason: insufficient_responses`. With no responses, `no_responses` and null shares.

### `GET /api/support`

**Sources:** `gold.fct_support_daily`, `gold.fct_agent_performance_daily`.

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `start`, `end` | The 90 days ending `data_end` |
| `plan_tier` | Optional |
| `call_type` | Validated against the call types in the data; filters only the AI-agent figures |
| `granularity` | `week` |

**`tickets`:**
- `created`, `resolved`.
- `resolved_to_created_ratio` (a count ratio: there is no ticket ID, so **no resolution time or backlog**).
- `active_user_days`, `tickets_per_1k_active_user_days`.

**`ai_agent`** and **`by_call_type[]`:**
- `calls` (AI-handled), `ai_resolution_rate`, `escalation_rate`, `human_handled_share`.
- `avg_csat`, `avg_handle_time_seconds` (weighted means over calls with a value).

### `GET /api/customer-health`

**Source:** `analytics.workspace_health_scores`, the latest snapshot, persisted by `analytics/health_scoring.py`. Tier bounds and weights come from the same module.

**Parameters:** `plan_tier`, the workspace's **current** plan.

**Response fields:**
- `snapshot_date`, `workspaces`, `mean_score`.
- `tiers[]`: all four tiers with bounds, counts and shares.
- `histogram[]`: 20 right-closed bins of 5 points, each labeled with its tier (a score of 40 is Critical).
- `by_plan[]`, `weights`.

### `GET /api/customer-health/workspaces`

Paginated workspace list.

**Parameters:**

| Parameter | Default / rule |
|---|---|
| `tier` | Optional |
| `plan_tier` | Optional |
| `sort` | `health_score` (allow-list: `health_score`, `mrr_usd`, `seat_count`, `active_users_30d`) |
| `order` | `asc` (or `desc`) |
| `limit` | 25 (1–100) |
| `offset` | 0 (0–10000) |

**Ordering:** the sort column, nulls last, then `workspace_id`, so pages are stable.

**Response:** `total`, `limit`, `offset`, `next_offset` (null on the last page) and `items[]`. An offset past the end returns `items: []`.
