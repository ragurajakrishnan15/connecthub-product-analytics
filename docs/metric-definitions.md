# Metric Definitions

Every metric below is computed from the warehouse; the defining model or
module is given so the definition can be checked against the code. Rates are
stored as percentages (0–100) in gold tables and validated as fractions
(0–1) by the data-quality checks.

## Engagement

| Metric | Definition | Source |
|---|---|---|
| **DAU** | Distinct users with any event on the day | `gold.fct_daily_active_users.dau` |
| **WAU (7d)** | Distinct users with any event in the 7 days ending on the day (inclusive) | `…wau_7d` |
| **MAU (28d)** | Distinct users with any event in the 28 days ending on the day | `…mau_28d` |
| **Active workspaces** | Distinct workspaces with any event on the day | `…active_workspaces` |
| **Session** | The generator's `session_id` (one user, one day, consecutive events); duration = last − first event timestamp | `intermediate.int_sessions` |

Every calendar day between the first and last event has a row (days with no
activity have DAU 0). WAU/MAU are checked against a brute-force distinct count
by `tests/test_dau_rolling_windows.sql`.

## Activation

| Metric | Definition | Source |
|---|---|---|
| **Placed first call / used AI / invited teammate** | User has a `call.started` / (`ai_assist.used` or `ai_voice_agent.activated`) / `team.member_invited` event with `event_date <= signup_date + 14` | `intermediate.int_activation_funnel` |
| **Activated (14d)** | All three milestones within 14 days of signup | `int_activation_funnel`, `gold.fct_experiment_user_metrics.activated_14d` |
| **14-day activation rate** | Activated users ÷ signups | semantic metric `activation_rate_14d` |

## Retention

| Metric | Definition | Source |
|---|---|---|
| **Cohort** | Users whose `first_active_date` (first product event, account events excluded) falls in the same Monday-start week | `gold.fct_retention_cohorts` |
| **Week-N retention** | Cohort users with any event in the Nth week after the cohort week ÷ cohort size, N = 0…12 | `…retention_rate` (percent) |
| **Pooled week-N retention** | Σ active users at week N ÷ Σ cohort sizes, over cohorts whose week N has fully ended by the as-of date | `analytics/cohort_engine.py summarize()` |

Week 0 is 100% by construction (the cohort is defined by activity in that week).

## Feature adoption

| Metric | Definition | Source |
|---|---|---|
| **Feature** | Event → feature mapping in `analytics/features.py` (mirrored in `int_feature_usage`) | |
| **Cumulative adoption at day D** | Users whose first use of the feature is ≤ D days after signup ÷ all users | `gold.fct_feature_adoption.cumulative_adoption_pct` |
| **AI feature adoption** | Active workspaces using `ai_assist` or `ai_voice_agent` ÷ active workspaces, in the query window | semantic metric `ai_feature_adoption` |

## Revenue

| Metric | Definition | Source |
|---|---|---|
| **MRR** | Per active seat: plan seat price × users with any event in the month (Free = $0; Essentials $15, Professional $25, Enterprise $45) | `gold.fct_workspace_mrr.mrr_usd` |
| **MRR movement** | Month over month: `new`, `expansion`, `contraction`, `churned` (to 0), `reactivation` (from 0), `retained`, `inactive` | `…mrr_movement` |
| **Revenue per paying workspace** | MRR ÷ workspaces with MRR > 0, per month | semantic metric `revenue_per_workspace` |
| **Revenue 60d (per user)** | Seat price of each month in which the user was active within 60 days of signup | `gold.fct_experiment_user_metrics.revenue_60d` |

## Customer health

Inputs (`gold.metrics_product_health`, one row per workspace at the latest event date):

| Input | Window | Direction |
|---|---|---|
| Active users ÷ seats (`dau_over_seats_ratio`) | 30 days | higher is better (weight 0.25) |
| Features adopted | 30 days | higher (0.20) |
| Share of calls resolved by the AI agent alone (`pct_ai_calls_automated`) | 30 days | higher (0.15) |
| Average session minutes | 30 days | higher (0.10) |
| NPS (−100…100) | 90 days | higher (0.10) |
| MRR change vs previous month | 1 month | higher (0.10) |
| Support tickets per active user | 30 days | lower (0.10) |

**Health score (0–100):** weighted average of each input's percentile rank across
workspaces; inputs a workspace has no data for (no calls, no NPS responses, no
billing) are dropped from its average rather than counted as zero.
**Tiers:** Critical < 40 ≤ At Risk < 55 ≤ Healthy < 70 ≤ Champion, calibrated by
backtest (scores as of Nov 30 vs. December churn; see PHASE_2_REPORT.md). Stored
in `analytics.workspace_health_scores` per snapshot date.

## NPS

Responses 0–10; promoters 9–10, passives 7–8, detractors 0–6.
**NPS = 100 × (promoters − detractors) ÷ responses.** (`staging.stg_nps_responses`)

## Experiments

| Metric | Definition | Source |
|---|---|---|
| **Assignment** | Registry (`experimentation/experiments.py`): traffic hash → in/out, salted variant hash → arm; `assigned_at` from data (signup, or first activity on/after start) | `experiments.experiment_assignments` |
| **Primary** | `activated_14d`, users with a complete 14-day window | `gold.fct_experiment_user_metrics` |
| **Guardrails** | `avg_session_minutes_14d`, `revenue_60d` (complete 60-day window); fail on a significant *decrease* | same |
| **SRM** | χ² test of arm sizes vs 50/50; p < 0.01 = mismatch (fails validation) | `experimentation/stat_tests.py` |
| **Decision** | SRM → HOLD; guardrail decrease → REVERT; significant lift → SHIP; Bayesian P(better) > 0.95 → SHIP, > 0.80 → CONTINUE; else REVERT | `experimentation/evaluate.py` |

Results per experiment are stored in `analytics.experiment_results`.
