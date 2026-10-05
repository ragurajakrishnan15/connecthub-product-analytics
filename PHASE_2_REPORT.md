# Phase 2 Report — Realistic Data and Real Metrics

**Date:** 2026-10-04
**Branch:** `phase-2` (from `870b853`)
**Scope:** `PHASE_1_REPORT.md` §9: rewrite the generator, add revenue and NPS to bronze/dbt, replace the simulations, make `fct_daily_active_users` scale, and replace the tautological tests. No frontend, Spark, Airflow, API or AI-analyst work.
**Environment:** Python 3.11.9 (`.venv`), PostgreSQL 15 via `docker compose`, dbt-core/dbt-postgres 1.7.4.

## Bottom line

| Check | Result |
|---|---|
| `pytest` | **64 passed, 0 skipped** (was 28). Integration tests ran against live Postgres. |
| `ruff check .` | All checks passed |
| `dbt build` | **PASS=74 WARN=0 ERROR=0**: 17 models + 57 tests (was 13 + 4) |
| Simulated or random values left in analytics | **None.** `evaluate_from_parquet` and parquet health scoring compute real metrics; two runs give identical output. |
| Warehouse vs parquet parity | **Experiment evaluation: identical.** Health inputs and retention: identical except ±0.01 rounding ties (explained below). |
| Planted experiment effect recovered | **Yes.** Activation 18.94% → 24.51% (+29.4% relative, 95% CI +3.9 to +7.2 pp, p < 1e-6). Guardrails pass, as designed. |
| Generator at the 500K-user default | **40.5M events in 153 s, 1.8 GB peak memory** (audit measured >23 GB / ~40 min) |
| Determinism | Same `--seed` → byte-identical files (tested); full pipeline reruns reproduce identical row counts |

All numbers below are from a clean run at the new dev default: `--users 10000` (1,000 workspaces, 783,615 events).

---

## 1. Synthetic data generator (rewritten)

`scripts/generate_synthetic_data.py` is now a behavioral model instead of uniform random noise.

**Causal structure:**
- **Workspace engagement.** Each workspace has a latent engagement level (shifted by plan). It drives its users' behavior, plan upgrades and downgrades, support-ticket rate, AI-agent resolution rate and NPS.
- **Ordered activation funnel.** First call → first AI feature use → first team invite, each milestone after the previous one. About 20% of callers find AI features later, outside the 14-day window.
- **Retention.** Daily activity decays from an early burst to a plateau until the user churns. Lifetimes are much longer for activated users, and a share of users (larger when activated) is retained long term.
- **Revenue.** Per-active-seat billing: each month a workspace pays its plan's seat price (Free $0, Essentials $15, Professional $25, Enterprise $45) for every user active that month. This is new as `subscriptions.parquet`.
- **NPS.** Survey responses at days 30/120/210/300 after signup, with a 35% response rate. Scores are driven by engagement, activation and support friction. New as `nps_responses.parquet`.
- **Agent evaluations.** One evaluation per `ai_voice_agent.call_handled` event, so every evaluation now has a `workspace_id`. Resolution quality depends on the workspace, and `resolved_by_ai` and `escalated_to_human` are now mutually consistent.
- **Planted experiment effect.** Users that `assign_variant(user_id, 'exp_onboarding_v2')` puts in `variant_1` are `EXPERIMENT_AI_LIFT = 1.30`× as likely to use an AI feature after their first call. Nothing else differs between variants.

**Engineering:**
- **Deterministic.** There is one seeded generator. Dimension IDs are UUID5 of `(seed, kind, index)`; event and evaluation IDs come from the seeded stream. The new `--seed` flag defaults to 42.
- **Vectorized and memory-bounded.** Activity is sampled as a user × day matrix per chunk of 20K users. Events are streamed to parquet with `pyarrow.ParquetWriter`.
- **Defaults.** `--users` defaults to 10,000 and `--workspaces` to users / 10. `--evaluations` is now an optional cap; the default is one evaluation per AI-handled call. `--out-dir` was added.
- **Compatibility.** All existing columns and event names are kept, so Spark, Great Expectations and LookML still match. `first_active_date` is now the first *product* use (account events excluded) and is NULL for users who never used the product: 1,092 of 10,000.

| Measured scale | Events | Time | Peak memory | events.parquet |
|---|---|---|---|---|
| 10,000 users | 783,615 | 3 s | 654 MB | 37 MB |
| 100,000 users | 8,247,262 | 32 s | 1.6 GB | 386 MB |
| 500,000 users | 40,485,941 | 153 s | 1.8 GB | 1.9 GB |

**What the data looks like (10K users, from the warehouse):**
- **MRR grows** from $1.8K (Jan) to $61.7K (Dec); revenue per paying workspace in December is $124.
- **Activation stages:** 2,129 fully activated / 1,284 AI-activated / 3,527 call-only / 3,060 signed up only.
- **Retention** (cohort by first-active week, pooled): week 1 75.1%, week 4 38.9%, week 8 26.3%, week 12 21.7%. Activated users retain about 3× better at week 4 (62.7% vs 21.4%).
- **Activity:** about 6.5 events per active day and ~12-minute average sessions.
- **NPS:** 5.6 overall.
- **AI agent outcomes:** 10,261 AI-resolved / 3,629 escalated / 2,320 human-handled.
- **Dec 31:** DAU 682, WAU 2,475, MAU 3,789.

## 2. Ingestion

`scripts/ingest_events.py`:
- **New tables:**
  - `bronze.subscriptions`: composite PK `(workspace_id, month_start)`, NUMERIC money columns.
  - `bronze.nps_responses`: `CHECK (score BETWEEN 0 AND 10)`.
  - `bronze.agent_evaluations.workspace_id`.
- **Streaming loads.** Parquet row groups go straight into `COPY` (`copy_parquet` / `copy_frames`), still one transaction per table, so memory no longer scales with file size. `copy_frame` is kept.
- **Schema drift.** A table whose columns differ from its spec is dropped (CASCADE) and recreated with a printed notice. Every load replaces bronze anyway, and dbt rebuilds the dependent views. This upgraded the existing Phase 1 `agent_evaluations` table automatically.
- **Load time.** All 7 tables at 10K users load in ~12 s.

## 3. dbt

**New models:**
- `stg_subscriptions`, `stg_nps_responses` (with `nps_category`).
- `gold.fct_workspace_mrr`: workspace × month MRR with previous MRR, change, and movement (`new` / `expansion` / `contraction` / `churned` / `reactivation` / `retained` / `inactive`).
- `gold.fct_experiment_user_metrics`: user grain, the single source for experiment evaluation.
  - `activated_14d`: primary metric.
  - `avg_session_minutes_14d`, `revenue_60d`: guardrails. `revenue_60d` is the seat price of each month the user was active within 60 days of signup.
  - `window_14d_complete` / `window_60d_complete`: so users whose window runs past the end of the data are excluded per metric.

**Changed models:**
- **`metrics_product_health`** adds:
  - `pct_ai_calls_automated` (AI-resolved ÷ all calls, 30 days)
  - `nps_score` (−100…100, 90 days; NULL without responses) and `nps_responses_90d`
  - `mrr_usd` and `mrr_change_usd` (snapshot month vs the month before)
- **`fct_daily_active_users`** is rewritten with a date spine and non-overlapping coverage intervals, using running sums of +1/−1 deltas. Its cost is now linear in user-days.
  - Verified **identical** to the old correlated-subquery model on all 364 days.
  - 6.6 s vs 12.8 s at 10K users; the gap widens with scale because the old cost grows with days × user-days.
  - It now emits a row for every calendar day.
- **`fct_agent_evaluations`** carries `workspace_id`.
- **Semantic layer.** `revenue_per_workspace` is restored: a new `workspace_revenue` semantic model on `fct_workspace_mrr`, with the `mrr` and `paying_workspaces` metrics.

**Tests (4 → 57):**
- **`models/schema.yml`:** unique / not_null on every key; relationships from events, users, subscriptions, NPS, evaluations and experiment metrics to their dimensions; accepted_values on plan tiers, variants, stages, movements and NPS categories.
- **Source tests** on the bronze keys.
- **Macro:** a local `unique_combination_of_columns` generic test (no dbt_utils dependency).
- **New singular tests:**
  - `test_dau_rolling_windows`: brute-force WAU/MAU on the 1st and 15th of every month.
  - `test_mrr_matches_active_seats`: billed seats = active users each month, and MRR = seats × price.
  - `test_health_metric_bounds`.

## 4. Python analytics

**`experimentation/evaluate.py`: the simulation is gone.**
- `evaluate_experiment(conn, id)` reads `gold.fct_experiment_user_metrics`.
- `evaluate_from_parquet(...)` computes the same per-user frame from parquet (`user_metrics_from_parquet`). Both feed `evaluate_user_metrics`, and the results are identical.
- **Guardrails are now real Welch t-tests on user-level values:** session duration (new) and revenue (previously faked with `np.random.normal`). Either one failing with a significant decrease gives REVERT.
- Signatures are kept; `users.parquet` and `subscriptions.parquet` default to the events file's directory.

**Result on `exp_onboarding_v2` (10K users):**

| | Control | Treatment | Result |
|---|---|---|---|
| Activation 14d (n = 4,619 / 4,851 with complete window) | 18.94% | 24.51% | +29.4% rel., p < 1e-6, **SHIP** |
| Avg session minutes 14d | 8.67 | 8.83 | +1.8%, p = 0.18 (guardrail OK) |
| Revenue 60d (n = 3,619 / 3,758) | $28.15 | $28.96 | +2.9%, p = 0.28 (guardrail OK) |
| SRM | 4,875 | 5,125 | χ² = 6.25, p = 0.012: **not flagged** (threshold 0.01) |

The SRM p-value is close to the threshold. That is chance in these particular seed-42 IDs: `assign_variant` hashes uniformly, and `test_balance` covers balance. I reported it rather than switching seeds to make it look cleaner.

**`analytics/health_scoring.py`: no random inputs, recalibrated.**
- **Same inputs on both paths.** The parquet path (`health_inputs_from_parquet`) now computes exactly the `metrics_product_health` inputs. The random NPS, revenue, AI-share and session values are gone. It takes an `as_of` date for backtests.
- **Scoring is a weighted average of percentile ranks (0–100).** The old min-max sum had problems:
  - It could not exceed 90.
  - One outlier compressed everyone else, which is why Phase 1 saw 182/200 Critical.
  - Inactive workspaces were rewarded for having zero tickets.
- **Inputs without signal** (no calls, no NPS responses, no billing) are skipped per workspace instead of counted as 0. Tickets are measured per active user.
- **Tier calibration by backtest.** Scored as of 2025-11-30, workspaces active in November, outcome = no active users in December:
  - AUC 0.70 (low score → churn).
  - Chosen cut-points 40 / 55 / 70 give monotonic churn: **Critical 13.5% · At Risk 6.3% · Healthy 3.8% · Champion 0.9%**.
  - December snapshot: 292 Critical (230 of them already have no active users) / 298 At Risk / 297 Healthy / 113 Champion.
- Raw input columns are now returned unmodified (previously overwritten with scaled values).

**Other changes:**
- **New `analytics/features.py`:** the shared event → feature map, mirroring `int_feature_usage`.
- **`analytics/cohort_engine.py`:** the parquet path cohorted by *signup* week while dbt uses *first-active* week, so the two disagreed. A new pure `retention_table()` matches `fct_retention_cohorts` and is used by `generate_retention_from_parquet`.
- **`experimentation/assignment.py`:** `assigned_at` is the user's signup time (onboarding experiment) instead of "now", so assignments are reproducible.

## 5. Tests (pytest 28 → 64)

| File | What it covers |
|---|---|
| `tests/conftest.py` | Session fixture: a 4,000-user generated dataset + assignments, built once (~1 s) |
| `test_generate_synthetic_data.py` (13) | Determinism, seed independence, ingestion column contract, referential integrity, date bounds, `first_active_date`, funnel ordering, planted effect, activated users retain better, MRR = active seats × price, NPS/CSAT ranges, sessions within one user-day |
| `test_cohort_engine.py` (7) | **Replaces the 3 tautological tests** (same names, now real). Exact cell values on a hand-built fixture (incl. Sunday week boundary, pre-cohort activity, users without a cohort), horizon cutoff, week 0 = 100%, falling pooled retention, parquet entry point |
| `test_evaluate.py` (6) | Detects a real lift; no lift → no SHIP; revenue guardrail and session guardrail each block a winner; incomplete windows excluded; parquet evaluation deterministic and JSON-serializable |
| `test_health_scoring.py` (8) | 0–100 range and ordering; tickets per active user; missing signals skipped; inactive workspace not rewarded; Free plan has no revenue signal; raw inputs preserved; parquet inputs one row per workspace; `as_of` backtest |
| `test_ingest_postgres.py` (+2) | New tables: composite PK and NPS check constraint enforced; table with the old layout recreated |
| `test_warehouse_parity.py` (3, integration) | Warehouse vs parquet: experiment evaluation (exact), health inputs, retention. Skips if `data/` doesn't match what is loaded |

**Parity tolerance:** rounded columns (`avg_session_duration_minutes`, `retention_rate`) may differ by exactly 0.01 on a few rows. PostgreSQL rounds NUMERIC half away from zero, while Python rounds a float that sits a hair below the tie. All counts and unrounded values match exactly.

## 6. Commands executed (final clean run)

```text
rm data/*.parquet
python scripts/generate_synthetic_data.py --users 10000     -> 783,615 events, 16,210 evals, 6,374 subscription rows, 2,099 NPS
python -m experimentation.assignment                        -> 4,875 / 5,125
python scripts/ingest_events.py --load-postgres             -> 7 tables loaded
cd dbt_project && dbt build                                 -> PASS=74 WARN=0 ERROR=0
pytest                                                      -> 64 passed
ruff check .                                                -> All checks passed!
python -m experimentation.evaluate                          -> SHIP, +29.4%
```

Also run: the generator at 10K/100K/500K users (time and peak working set, table in §1); the old vs new DAU model on the same data (identical, 12.8 s vs 6.6 s); and the health backtest (§4).

## 7. Files changed

**Added (16):**
- `PHASE_2_REPORT.md`, `analytics/features.py`
- dbt models: `staging/stg_subscriptions.sql`, `staging/stg_nps_responses.sql`, `gold/fct_workspace_mrr.sql`, `gold/fct_experiment_user_metrics.sql`, `models/schema.yml`
- dbt tests and macro: `macros/test_unique_combination_of_columns.sql`, `tests/test_dau_rolling_windows.sql`, `tests/test_mrr_matches_active_seats.sql`, `tests/test_health_metric_bounds.sql`
- pytest: `conftest.py`, `test_generate_synthetic_data.py`, `test_evaluate.py`, `test_health_scoring.py`, `test_warehouse_parity.py`

**Modified (17):**
- Scripts: `scripts/generate_synthetic_data.py` (rewrite), `scripts/ingest_events.py`
- Python: `experimentation/evaluate.py` (rewrite), `experimentation/assignment.py`, `analytics/health_scoring.py` (rewrite), `analytics/cohort_engine.py`
- dbt models: `fct_daily_active_users.sql` (rewrite), `metrics_product_health.sql`, `fct_agent_evaluations.sql`, `semantic/metrics_product_health.yml`, `staging/sources.yml`
- Tests: `tests/test_cohort_engine.py` (real tests), `tests/test_ingest_postgres.py` (`workspace_id` in fixture + 2 tests)
- Other: `lookml/views/product_health.view.lkml` (AI automation, NPS, MRR measures), `docs/semantic_layer_guide.md`, `README.md`, `Makefile` (`make generate` = 10K users + assignment)

**Not touched:**
- `index.html`
- `spark_jobs/*`, `dags/*`, `great_expectations/*`
- Hex notebooks; Jupyter notebooks (their calls still work unchanged)
- other LookML views

## 8. Remaining issues

| # | Issue | Notes |
|---|---|---|
| 1 | **The health score is relative.** Percentile ranks depend on which workspaces are scored together, and the tiers are calibrated on synthetic data. | Fine for ranking outreach; recalibrate on real data. A logistic model trained on the churn backtest would be the next step. |
| 2 | **SRM p = 0.012 at seed 42** | Chance, not a bug; see §4. Audit item 16 (salted second hash for the traffic bucket) is still open. |
| 3 | **Revenue attribution is a modeling choice.** Revenue is per *active* seat, and `revenue_60d` credits a user with their seat price for each active month. | Documented in the model SQL. |
| 4 | **`stg_events` is a view with a `ROW_NUMBER` dedupe.** It is recomputed by every downstream model; `int_sessions`/`int_feature_usage` take ~40 s at 10K users when run in parallel. | Materialize it as a table or make it incremental in Phase 4. |
| 5 | **Still mocked:** the `index.html` numbers and AI analyst; the Hex `experiment_writeup` narrative is hard-coded | Phases 8–9 |
| 6 | **Carried over from Phase 1:** Airflow image and DAG, Spark untested, Great Expectations unwired, LookML unvalidated, notebooks need CWD = repo root, nested folder | Unchanged |

## 9. Recommended next phase

The audit's remaining backend items before serving:
1. Materialize `stg_events` and the intermediate models incrementally.
2. Salted second hash for the traffic bucket in assignment, and persist assignments from `assign_experiment_cohort`.
3. A `__main__` CLI for `cohort_engine` and `evaluate` (the DAG calls them with `--date`).
4. Orchestration (audit Phase 7), so the whole flow above runs as one DAG.
