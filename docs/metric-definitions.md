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
| **Observed adoption at day D** (censoring-aware) | Of users with at least D days of history (`signup_date + D ≤ last event date`), those whose first use is ≤ D days after signup ÷ those users. NULL when no user has D days of history. | `gold.fct_feature_adoption.observed_adoption_pct` (`eligible_adopters` / `eligible_users`) |
| **AI feature adoption** | Active workspaces using `ai_assist` or `ai_voice_agent` ÷ active workspaces, in the query window | semantic metric `ai_feature_adoption` |

`cumulative_adoption_pct` is kept unchanged. It divides by every user, including users who
signed up too recently to be observed at day D, so it **understates** adoption at later
days (right-censoring). At 10K users, day-90 AI Assist adoption is 32.53% cumulative vs
34.65% observed: only 6,268 of 10,000 users have 90 days of history. Use
`observed_adoption_pct` for "what share of users adopt by day D"; it is not redefined
silently — both columns exist and are named for what they measure.

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

## Serving models (read by the API)

Small gold tables in `dbt_project/models/gold/serving/` (dbt tag `serving`). They hold
**additive counts and sums** at daily or monthly grain, so any date range re-aggregates
exactly and ratios are computed from sums, never by averaging averages. Their size depends
on calendar days × plan tiers, not on the number of users, so API requests never scan raw
events. Each has an enforced dbt contract (column names, types, NOT NULL), a grain test,
reconciliation tests (`dbt_project/tests/serving/`) and Great Expectations gold checks.

### Conventions

| Convention | Rule |
|---|---|
| **Plan tier** | The plan the workspace was **billed on in the month of the activity** (`stg_subscriptions`), falling back to the workspace's current plan when that month has no subscription row. `stg_users.plan_tier` is the current plan; 1,418 of 10,000 users signed up on a different plan. |
| **"Latest" and completeness** | Always relative to the **last loaded event date** (`MAX(stg_events.event_date)`), never the wall clock: the data is a synthetic 2025. A static test fails if any model uses `current_date` / `now()`. |
| **Incomplete periods** | Flagged, not dropped: `window_14d_complete` (activation), `month_complete` (monthly tables). Consumers compute rates over complete periods by default. |
| **Ratios** | Not stored (except the experiment curve's rate, whose denominator is never 0). A ratio with a zero denominator is undefined (NULL), never 0. |

### Definitions

| Metric | Definition | Source |
|---|---|---|
| **Funnel steps (strict)** | Signed up → placed first call → call **and** AI feature → all three (= activated). Each step requires the previous ones, all within 14 days of signup. | `fct_activation_daily` (`signups`, `placed_first_call`, `call_and_ai`, `activated_14d`) |
| **Milestone rates (independent)** | Users reaching each milestone within 14 days ÷ signups, regardless of the other milestones (a user can invite without using AI). | `fct_activation_daily` (`placed_first_call`, `used_ai_feature`, `invited_team_member`) |
| **14-day activation rate** | Σ `activated_14d` ÷ Σ `signups` over signup dates with `window_14d_complete`. A window is complete when `signup_date + 14 ≤ last event date` (inclusive). Users in incomplete windows may still activate, so including them biases the rate down; they are counted but flagged. | `fct_activation_daily` |
| **Time to milestone** | Days from signup to the first occurrence of the milestone, among users who reached it within 14 days (so 0–14, conditional on reaching it). `activated_14d` = the day the last of the three was reached. Integer days make medians exact. | `fct_activation_milestone_days` |
| **MRR, paying workspaces, seats** | Σ `mrr_usd`; workspaces with MRR > 0; Σ billed seats, per month and plan. | `fct_revenue_monthly` |
| **ARPA** (revenue per paying workspace) | MRR ÷ paying workspaces (semantic metric `revenue_per_workspace`). | `fct_revenue_monthly` |
| **MRR movement** | Σ `mrr_change_usd` per movement type: new, expansion, reactivation (≥ 0); contraction, churned (≤ 0). They sum to MRR − previous MRR. A workspace that changes tier moves all its MRR to the new tier, so the month-over-month identity holds for the all-plans total, not per tier. | `fct_revenue_monthly` |
| **NPS** | 100 × (promoters − detractors) ÷ responses over the chosen dates (promoter 9–10, passive 7–8, detractor 0–6). | `fct_nps_daily` |
| **Support tickets** | Counts of `support.ticket_created` / `support.ticket_resolved` events. | `fct_support_daily` |
| **Tickets per 1,000 active user-days** | 1,000 × tickets created ÷ Σ daily active users over the range. Daily active users summed over days are active user-days, which are additive (distinct users over a range are not). | `fct_support_daily` |
| **Ticket resolution time, backlog** | **Not available.** Events have no ticket ID, so a resolution cannot be matched to its ticket. `tickets_resolved ÷ tickets_created` is a count ratio, not a per-ticket resolution rate. | — |
| **AI resolution rate** | AI-handled calls resolved by the agent without escalation ÷ AI-handled calls. Not the same as `pct_ai_calls_automated` (health input), whose denominator also includes human-placed calls. | `fct_agent_performance_daily` |
| **Escalation rate / human-handled share** | Escalated (any call flagged `escalated_to_human`, even if also `resolved_by_ai`) ÷ calls; neither ÷ calls. | `fct_agent_performance_daily` |
| **Average CSAT / handle time** | `csat_sum ÷ csat_count`, `handle_time_seconds_sum ÷ handle_time_count` (weighted over calls with a value). A missing call type is reported as `unknown`. | `fct_agent_performance_daily` |
| **Monthly active users / workspaces** | Distinct users / workspaces with any event in the calendar month (account events included). | `fct_activity_monthly` |
| **AI feature adoption (monthly)** | `ai_active_workspaces ÷ feature_active_workspaces`: workspaces using an AI feature ÷ workspaces using any mapped feature, as in the semantic metric. | `fct_activity_monthly` |
| **Feature usage (monthly)** | Distinct users / workspaces using each feature in the month; `usage_events` = mapped events. | `fct_feature_usage_monthly` |
| **Experiment activation curve** | For participants with a complete 14-day window: cumulative share activated by day D since signup (D = 0–14), per arm. Day 14 equals the evaluated primary metric. | `fct_experiment_activation_curve` |

### Retention cells with zero active users

`fct_retention_cohorts` has **no row** for a cohort week in which nobody was active
(the cohort → activity join only produces weeks with activity). This is unchanged, and
the behaviour is defined as: **a missing cell whose week has ended is 0% retention; a
missing or present cell whose week has not ended is unknown.** `analytics/cohort_engine.py`
already implements this (`as_of` keeps only cells complete by the as-of date, `summarize`
sums active users over every eligible cohort, so a missing cell contributes 0), and
`tests/test_serving_fixture.py` checks it on a cohort with zero active users in weeks 4–8.
The API (Phase 4B) must zero-fill complete missing cells when it returns the matrix. At 10K
users no complete cell is missing; the case appears with small cohorts or filters.
