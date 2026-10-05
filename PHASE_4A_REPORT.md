# Phase 4A Report: Warehouse / API Data Foundation

**Date:** 2026-10-05 · **Branch:** `phase-4` (plan committed as `7c11076`; Phase 4A changes are **uncommitted**, pending review)
**Scope:** The API-serving gold models from `PHASE_4_PLAN.md` §3, with tests, data-quality checks, documentation and benchmarks.
**Not done (by design):** FastAPI, routers, `index.html`, the AI analyst, deployment.

**Environment:**
- Windows 11, Python 3.11.9 (`.venv`), PostgreSQL 15 (Docker), dbt-core/dbt-postgres 1.7.4
- Development dataset: 10,000 users, seed 42 (781,811 events, calendar 2025)
- The Docker VM was shared with 5 running containers from other projects, so timings vary (see §6).

## Bottom line

| Check | Result |
|---|---|
| Live schema vs plan §0.1 | **Matches.** Every relation and column the plan relies on exists with the expected types. The only difference is that the data window is 2025-01-02 → 2025-12-31 (364 days, not 365), which doesn't affect the design. |
| `dbt build` | **PASS=106 WARN=0 ERROR=0** (26 materialized models + 80 tests; was 74 = 17 + 57) |
| `pytest` | **122 passed, 2 skipped** (was 96 + 2). The skips are the Airflow and Spark container-only tests, as in Phase 3. The end-to-end test ran. |
| `ruff check .` | All checks passed |
| Great Expectations, gold stage | **71/71** (was 24) |
| Independent recomputation | All 10 models equal a pandas computation from parquet (exact counts; money and rates to 1e-6) |
| Incremental == full refresh | **Identical on 35 relations** (`verify-incremental` on real data), and on the hand-built fixture |
| Idempotency | Two full pipeline runs: **identical fingerprints**, 35 relations, 2,112,759 rows |
| Tests can fail | The parity tests and the 4 serving dbt tests each fail on deliberately corrupted rows |
| Secrets | No `.env` secret value in any of the 23 changed or new files |

---

## 1. Step 1: infrastructure and schema confirmation

1. **Docker:** started Docker Desktop and brought up only this project's `postgres`, not Airflow.
2. **Pipeline:** ran the existing pipeline unchanged (`python -m pipeline run --users 10000`, run `phase4a-baseline`). All 8 steps succeeded, rc 0, in 96 s.
3. **Schema:** compared `information_schema` with plan §0.1. All 19 relations the plan reads exist with the planned columns, and row counts match the plan's approximations.
4. **Data checks that shaped the models:**

| Check | Result | Consequence |
|---|---|---|
| Complete retention cells missing from `fct_retention_cohorts` | 0 of 598 at 10K users, plus 13 incomplete cells present | The zero-cell case is latent at this scale. It is handled and tested anyway (§3). |
| Gaps in `stg_subscriptions` workspace-months | 0 | The month-over-month MRR identity can be tested strictly |
| Events, NPS responses and evaluations without a subscription row for their month | 0 / 0 / 0 | The billed-plan join is complete. The fallback to the current plan exists but is not exercised on this data. |
| `stg_users.plan_tier` ≠ plan billed in the signup month | **1,418 of 10,000** | `stg_users.plan_tier` is the workspace's *current* plan, so the serving models use the billed plan of the activity month. |
| Milestone flag set but first date > signup + 14; first dates before signup | 0; 0 | Days to milestone are always 0–14 |
| AI without a call; invite without call ∧ AI | 0; 0 | The funnel happens to be nested in this data. The strict definition is still used, and the fixture tests a non-nested user. |
| Signups whose 14-day window is incomplete | 530 | Flagged with `window_14d_complete`, not dropped |

## 2. Step 2: models added

All the new tables are in schema `gold`, under `dbt_project/models/gold/serving/`, with dbt tag `serving`:
- **Contract:** every model has an **enforced dbt contract**. dbt checks column names and types before building, and every column is `NOT NULL` in the database.
- **Grain:** every model has a grain-uniqueness test.
- **No wall clock:** "latest" and "complete" always come from `MAX(stg_events.event_date)`.

| Model | Grain | Materialization | Rows (10K) | Built from |
|---|---|---|---|---|
| `fct_activation_daily` | signup_date × plan | table | 1,372 | `int_activation_funnel` (via `int_user_signup_plan`) |
| `fct_activation_milestone_days` | signup_date × plan × milestone × days (0–14) | table | 11,123 | same |
| `fct_revenue_monthly` | month × plan | table | 48 | `fct_workspace_mrr` |
| `fct_nps_daily` | response_date × plan | table | 880 | `stg_nps_responses` |
| `fct_support_daily` | event_date × plan | **incremental**, whole dates | 1,434 | `stg_events` |
| `fct_agent_performance_daily` | call_date × plan × call_type | table | 4,017 | `fct_agent_evaluations` |
| `fct_feature_usage_monthly` | month × feature | **incremental**, whole months | 84 | `int_feature_usage` |
| `fct_activity_monthly` | month | **incremental**, whole months | 12 | `stg_events`, `int_feature_usage` |
| `fct_experiment_activation_curve` | experiment × variant × day (0–14) | table | 60 | `fct_experiment_user_metrics`, `int_activation_funnel` |

**Supporting changes:**
- **`int_user_signup_plan`** (new, ephemeral, so it creates no relation): a user's milestones plus the plan billed in the signup month. It is shared by the two activation models.
- **`fct_feature_adoption`** (existing model, extended additively):
  - New columns `eligible_users`, `eligible_adopters` and `observed_adoption_pct`, a censoring-aware adoption curve.
  - `cumulative_adoption_pct` and the other existing columns are **unchanged**, as are their values.

**Incremental design:**
- `fct_support_daily` uses `unique_key = event_date`, not the full grain, so a reprocessed date is deleted and rebuilt whole. A plan with no events on a reprocessed date can't leave a stale row.
- The monthly models recompute every month that overlaps the lookback window, because a month's distinct counts need all of its rows.
- The table models read small inputs (≤ 16K rows at 10K users), so incrementality wouldn't pay off.

**Optional model 10 (`fct_retention_by_week1_features`): not built.** The dashboard panel it would back ("Retention by Features Adopted (Week 1)") shows fabricated numbers, and no headline KPI or other panel depends on it. Per your instruction I didn't build it. Phase 5 can hide the panel, or the model can be added later.

## 3. Metric definitions and the known issues

The full definitions are in `docs/metric-definitions.md` → "Serving models (read by the API)". How each known issue is handled, without silently redefining any existing metric:

| Issue | Chosen behavior | Where it is enforced / tested |
|---|---|---|
| **Retention cells with zero active users** | `fct_retention_cohorts` is unchanged. The rule: a missing cell whose week has ended = 0%; a cell whose week hasn't ended = unknown. The existing `cohort_engine.as_of` / `summarize` already behave this way. The API must zero-fill the matrix (Phase 4B). | `test_serving_fixture.py::test_retention_zero_cells_and_completeness`: a cohort with zero active users in weeks 4–8 gives pooled week 4 = 0.0 and week 8 = 0.0, and the incomplete week 2 is excluded. |
| **Activation without a full 14-day window** | Counted but flagged (`window_14d_complete`, boundary inclusive: `signup_date + 14 ≤ last event date`). Rates use complete windows by default. The experiment curve uses complete windows only, matching `evaluate.py`. | dbt test `window_flag`; fixture users on `data_end − 14` (complete) and `data_end − 13` (incomplete, activated, excluded from the curve) |
| **Feature adoption without enough observation time** | The existing column is kept and documented as right-censored. A new, explicitly named `observed_adoption_pct` counts only users with ≥ D days of history, and is NULL when there are none. | Parity test (pandas); fixture with eligible counts 5/4/4/3/2/2/0 at D = 0/1/13/14/15/63/64; NULL at D ≥ 64 |
| **Missing ticket IDs** | Only counts are stored. Resolution time and backlog are documented as **not available**; `resolved ÷ created` is documented as a count ratio. | `docs/metric-definitions.md`; model comment |
| **Dataset dated 2025, not today** | Every model derives "latest" and "complete" from `MAX(stg_events.event_date)`. | `test_serving_static.py::test_no_model_uses_the_wall_clock` scans every model for `current_date`, `now()` and similar |

**Further definition decisions** (each documented):
- **Plan tier:** the billed plan of the activity month.
- **Strict funnel vs independent milestone rates:** both are stored.
- **AI resolution rate:** a share of *AI-handled* calls. This is distinct from the health input `pct_ai_calls_automated`.
- **AI adoption denominator:** workspaces using any mapped feature, matching the existing semantic metric `ai_feature_adoption`.
- **Tickets per 1,000 active user-days:** this denominator is additive across days, whereas distinct users over a range are not.

## 4. Step 3: validation results

### 4.1 Independent recomputation (`tests/test_serving_models.py`, 10 tests, real 10K warehouse)

Each model is recomputed in pandas from `data/*.parquet`, sharing no SQL with dbt, and compared row by row: grain keys exactly, counts exactly, money and rates to 1e-6. **All 10 pass.** Two further checks in the warehouse:
- The experiment curve at day 14 equals the persisted evaluation's activation rates:
  - `exp_onboarding_v2`: 0.186746 / 0.253845
  - `exp_ai_summary_v1`: 0.246832 / 0.280720
- Daily active users summed across plans equal `fct_daily_active_users.dau` on **every** day.

### 4.2 Edge cases on a hand-built warehouse (`tests/test_serving_fixture.py`, 13 tests)

This uses a throwaway database (`<db>_serving_fixture`) with 5 users, 2 workspaces and 23 events, chosen so that every expected value is worked out by hand:
- **Window boundaries:** a milestone on day 14 counts and day 15 doesn't.
- **Non-nested user:** one user invited a teammate without using AI.
- **Billed plan:** differs from the current plan.
- **MRR movements:** all of new, expansion with a tier change, contraction, reactivation, churn and inactive.
- **NPS boundaries:** scores 6/7/8/9.
- **Agent outcomes:** a call flagged both resolved and escalated counts as escalated; a missing call type becomes `unknown`.
- **Zero denominator:** `observed_adoption_pct` is NULL, not 0.

**Sensitivity:** after building, the test changes bronze:
- an AI event on day 13
- a resolved ticket
- a detractor NPS response
- higher March MRR for one workspace

It then runs an **incremental** `dbt run` and asserts the exact deltas in 6 models. A `--full-refresh` then gives **identical fingerprints** for all 10 models.

**Detection:** corrupting one gold row per model makes each of the 4 serving dbt tests **fail**.

### 4.3 Proof the tests aren't vacuous (real warehouse)

I corrupted one row in `fct_nps_daily` and one in `fct_support_daily`:
- The parity tests failed (`AssertionError: passives`, `AssertionError: tickets_created`).
- After `dbt run --full-refresh` of those two models, they passed again.

### 4.4 dbt, GE and pipeline

| Command | Result |
|---|---|
| `dbt compile` | 27 models (incl. 1 ephemeral), 80 tests, no errors |
| `dbt build` (full project) | **PASS=106 WARN=0 ERROR=0**, 15.9 s |
| `dbt test --select tag:serving` | 23/23 pass: 19 generic + 4 singular |
| `python -m quality.validate --stage gold` | **71/71** (47 new: PK, ranges, funnel order, signups total, revenue signs and identity, NPS/agent reconciliation, CSAT mean in [1, 5], support vs DAU, activity order, curve bounds, observed adoption in [0, 1]) |
| `python -m pipeline verify-incremental` (real 10K data) | `identical: true`, **35 relations compared**, `differing: []` |
| Pipeline run twice (`phase4a-run-1`, `-2`) | rc 0 both; **fingerprints identical** (35 relations, 2,112,759 rows). Run 2: generate skipped, 0 assignments inserted, event reload skipped, dbt incremental. |
| `pytest` (incl. end-to-end in `<db>_e2e`) | **122 passed, 2 skipped** (container-only), 294 s |
| `ruff check .` | All checks passed |

**dbt tests added:**
- **Generic (19):** a grain-uniqueness test for each of the 9 models, plus accepted values for plan, milestone, call type, feature and variant.
- **Singular (4, in `dbt_project/tests/serving/`):**
  - funnel order
  - signup and activated totals
  - window flag
  - milestone histogram vs funnel totals
  - days in 0–14
  - revenue signs, movement identity, cross-month continuity, first month and totals
  - NPS categories and totals
  - agent outcome reconciliation and CSAT range
  - ticket totals
  - support vs DAU
  - activity ordering
  - feature ≤ activity totals
  - `month_complete` flag
  - curve monotonicity and day 14 vs the user-level metrics
  - observed-adoption monotonicity and bounds
  - day-0 eligibility

## 5. Step 5: tests added

| File | Kind | Tests | Covers |
|---|---|---|---|
| `tests/test_serving_static.py` | unit (no DB) | 3 | No wall clock in any model; every serving model has an enforced contract, typed NOT NULL columns and a grain test; tag config |
| `tests/test_serving_models.py` | integration | 10 | Correctness vs independent pandas; curve vs persisted evaluation |
| `tests/test_serving_fixture.py` | integration | 13 | Schema and contracts on a fresh DB; exact values; edge cases; date boundaries; zero denominators; retention zero cells; sensitivity; incremental == full; the tests detect corruption |

Your checklist maps to these tests:
- **Schema:** contracts and static test
- **Correctness:** parity and fixture
- **Edge cases:** fixture
- **Reconciliation:** dbt singular tests, GE and fixture
- **Funnel ordering:** dbt, GE and fixture
- **No negative revenue:** dbt signs test, GE and fixture
- **Valid ratios:** GE ranges, dbt bounds and NULL on zero denominators
- **Date boundaries:** window and month flags, first and last day

## 6. Step 4: performance (10K users only)

### 6.1 Build time (dbt `run_results.json`, single runs)

| Run | dbt elapsed | Models / tests | Σ model time | New serving models + adoption |
|---|---|---|---|---|
| Baseline before Phase 4A (pipeline run, incremental) | 20.0 s | 17 / 57 | 27.8 s | — (`fct_feature_adoption` 0.55 s) |
| After, full refresh (`phase4a-run-1`) | 22.9 s | 26 / 80 | 43.4 s | 9.48 s |
| After, incremental (`phase4a-run-2`) | 13.6 s | 26 / 80 | 28.9 s | 5.35 s |

| Model | Full refresh | Incremental | Rows written (full → incremental) |
|---|---|---|---|
| `fct_activity_monthly` | 3.25 s | 1.67 s | 12 → 2 (Nov + Dec recomputed) |
| `fct_support_daily` | 2.70 s | 0.67 s | 1,434 → 16 (4 dates × 4 plans) |
| `fct_feature_adoption` (with new columns) | 1.53 s | 1.27 s | 637 |
| `fct_feature_usage_monthly` | 0.84 s | 0.64 s | 84 → 14 |
| `fct_agent_performance_daily` | 0.33 s | 0.30 s | 4,017 |
| `fct_activation_milestone_days` | 0.19 s | 0.18 s | 11,123 |
| `fct_activation_daily`, `fct_revenue_monthly`, `fct_nps_daily`, `fct_experiment_activation_curve` | 0.12–0.18 s each | same | 1,372 / 48 / 880 / 60 |

**How to read these numbers:**
- **Cost:** the serving layer adds about 9.5 s of model time on a full refresh and about 5.4 s incrementally.
- **Elapsed time is unreliable:** dbt elapsed is shorter after the change than before (13.6 s vs 20.0 s) despite more work. Wall-clock timings on this shared VM vary by ±30% (as Phase 3 found), so only the per-model times above are meaningful.
- **Extra cost of the adoption columns:** about 1 s, at the same 637 rows.
- **Pipeline totals:** 145 s for a full-reload run, 77 s for an incremental rerun. GE gold validation takes 22 s, up from about 14 s, because of the 47 new checks.

### 6.2 Query characteristics: serving table vs computing the same answer upstream

Median of 5 `EXPLAIN ANALYZE` runs on the 10K warehouse. **The answers are identical in every case.**

| API-style query | Serving: time / rows read | Upstream: time / rows read |
|---|---|---|
| Support tickets by week, last 90 days | **0.22 ms** / 360 | 39.25 ms / `stg_events` scan |
| AI feature adoption, December | **0.02 ms** / 1 | 41.94 ms / 44,433 (`int_feature_usage`) |
| Activation funnel, 90 signup days | **0.15 ms** / 357 | 1.26 ms / 3,616 |
| MRR by plan, 12 months | **0.03 ms** / 48 | 1.87 ms / 6,374 |
| NPS, last 90 days | **0.10 ms** / 331 | 0.36 ms / 987 |
| AI agent resolution by month | **1.46 ms** / 4,017 | 6.48 ms / 16,122 |

- **Size:** serving tables grow with calendar days × plans (× call types), **not with users**. The upstream inputs grow with users and events.
- **Untested beyond 10K:** I did **not** measure 100K or 500K. Phase 3 showed 100K ingestion doesn't fit this host's memory and 500K doesn't fit its disk. No claim is made about performance at those scales.

## 7. Files changed

**Added (18):**
- **Models:** `dbt_project/models/gold/serving/` (9 models + `serving.yml`) and `dbt_project/models/intermediate/int_user_signup_plan.sql`.
- **dbt tests:** `dbt_project/tests/serving/` (4 singular tests).
- **pytest:** `tests/test_serving_static.py`, `tests/test_serving_models.py`, `tests/test_serving_fixture.py`.
- **Docs:** `PHASE_4A_REPORT.md`.

**Modified (5):**
| File | Change |
|---|---|
| `dbt_project/dbt_project.yml` | `serving` tag for `models/gold/serving/` |
| `dbt_project/models/gold/fct_feature_adoption.sql` | Additive censoring-aware columns; existing columns and values unchanged |
| `quality/expectations.py` | 47 gold checks for the serving models |
| `docs/metric-definitions.md` | Serving-model conventions and definitions; observed adoption; retention zero-cell rule |
| `docs/architecture.md` | Gold schema row and incremental list mention the serving models |

**Not touched:** `index.html`, the API (doesn't exist yet), the DAG, the pipeline steps, the existing analytics and experimentation code, the existing dbt models other than `fct_feature_adoption`.

## 8. Known limitations

1. **Benchmarks are 10K only.** 100K and 500K weren't run; see Phase 3 §10 for why. Timings are single runs on a shared Docker VM.
2. **The Airflow image wasn't rebuilt.** It bakes the project code in, so the DAG will build the new models only after `docker compose build`. I didn't change or run the DAG in 4A.
3. **Late data:** the incremental serving models inherit the Phase 3 rule. Late events older than `lookback_days` (3) need `--full-refresh`, which a full reload of different data triggers automatically.
4. **Retention zero cells stay implicit in gold.** Zero-filling is the consumer's job: the existing Python functions handle it correctly, and the API must handle it in 4B.
5. **Per-tier MRR movement:** a workspace that changes tier moves all its MRR to the new tier, so the cross-month identity holds only for the all-plans total. This is documented and tested at that level.
6. **Uniqueness isn't a database constraint.** Grain uniqueness is enforced by dbt and GE tests, not a `PRIMARY KEY`. I left out PK constraints because dbt's table swap can collide on constraint index names; I did not test that.
7. **A pre-existing gap I noticed:** `int_feature_usage` (Phase 3) deletes and re-inserts by its full grain key. A feature that vanishes from a reprocessed date could therefore leave a stale row. The new incremental models avoid this by keying on whole dates or months. `int_feature_usage` is unchanged.
8. **No API grants yet.** The read-only role doesn't exist until Phase 4B, so dbt `+grants` aren't configured.
9. **Optional model 10 not built** (§2).

## 9. Remaining blockers

None for Phase 4B. Before Phase 4B can be validated in Docker, the Airflow image needs a rebuild (limitation 2), but that isn't needed for the local API work.

## 10. Exact next step

Commit Phase 4A after your review. Then start **Phase 4B = `PHASE_4_PLAN.md` §14 steps 2–3**:
- **Small refactors:**
  - `analytics/retention.py` with no plotting imports, and `summarize(weeks=…)`
  - `database_url(user, password)`
  - registry `kind` / `hypothesis` fields
  - `decision_code`
  - health-score index
- **API skeleton:**
  - settings
  - pooled read-only engine
  - `python -m api.provision` (read-only role + dbt `+grants`)
  - problem+json errors
  - JSON request logging
  - `/api/health`, `/api/health/ready`, `/api/meta`
  - their tests, including the read-only role and grants surviving a `dbt build`

No data endpoints until the skeleton is reviewed.
