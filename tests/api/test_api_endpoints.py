"""Integration: every data endpoint against the built local warehouse.

Values are compared with independent SQL, written against upstream relations
(stg_events, int_activation_funnel, fct_workspace_mrr, stg_nps_responses,
fct_agent_evaluations, int_feature_usage, ...) wherever possible rather than
the serving table the API reads. Also: date filtering and boundaries,
incomplete periods, empty results, zero denominators, pagination, sorting,
invalid and unsupported parameters, injection attempts, response schemas, and
that the API never changes the warehouse.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from api_testlib import assert_problem, client_for, warehouse_settings
from sqlalchemy import text

from api.provision import provision
from api.schemas.activation import ActivationData
from api.schemas.adoption import AdoptionData
from api.schemas.common import Envelope
from api.schemas.customer_health import HealthSummary, WorkspacePage
from api.schemas.engagement import EngagementData
from api.schemas.experiments import ExperimentDetail, ExperimentList
from api.schemas.nps import NpsData
from api.schemas.overview import OverviewData
from api.schemas.retention import CohortMatrix, RetentionData
from api.schemas.revenue import RevenueData
from api.schemas.support import SupportData
from pipeline import config
from pipeline.fingerprint import fingerprint

pytestmark = pytest.mark.integration
APPROX = 1e-6
# billed plan of the month: the serving models' plan definition, re-derived here
PLAN_JOIN = """LEFT JOIN staging.stg_workspaces w ON w.workspace_id = {ws}
  LEFT JOIN staging.stg_subscriptions s ON s.workspace_id = {ws}
   AND s.month_start = DATE_TRUNC('month', {d})::date"""


@pytest.fixture(scope='module')
def wh():
    settings = warehouse_settings()
    owner = config.create_engine(settings.postgres_db)
    try:
        with owner.connect() as conn:
            conn.execute(text('SELECT 1 FROM gold.fct_activation_daily LIMIT 1'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'warehouse not built: {exc}')
    provision(owner, settings.api_db_user, settings.api_db_password.get_secret_value())
    before = fingerprint(owner, schemas=('gold', 'analytics'))
    with owner.connect() as conn:
        start, end = conn.execute(text(
            'SELECT MIN(event_date), MAX(event_date) FROM staging.stg_events')).one()
    with client_for(settings) as client:
        yield SimpleNamespace(client=client, owner=owner, start=start, end=end, before=before)
    owner.dispose()


def q(wh, sql, **params):
    with wh.owner.connect() as conn:
        return conn.execute(text(sql), params).mappings().all()


def q1(wh, sql, **params):
    return q(wh, sql, **params)[0]


def get(wh, path, model, **params):
    r = wh.client.get(path, params=params)
    assert r.status_code == 200, r.text
    return Envelope[model].model_validate(r.json())     # response schema validation


def close(a, b, tol=APPROX):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


# ================================================================ overview
def test_overview_kpis_equal_independent_sql(wh):
    env = get(wh, '/api/overview', OverviewData)
    k, end = env.data.kpis, wh.end
    assert env.meta.as_of == end and env.meta.data_version

    mrr = q(wh, """SELECT month_start, SUM(mrr_usd) AS mrr FROM gold.fct_workspace_mrr
                   GROUP BY 1 HAVING (month_start + INTERVAL '1 month' - INTERVAL '1 day')::date
                   <= :end ORDER BY 1 DESC LIMIT 2""", end=end)
    assert (k.mrr_usd.value, k.mrr_usd.previous_value) == (float(mrr[0]['mrr']), float(mrr[1]['mrr']))
    assert k.mrr_usd.period.start == mrr[0]['month_start']

    def dau(d):
        return q1(wh, 'SELECT COUNT(DISTINCT user_id) AS n FROM staging.stg_events '
                      'WHERE event_date = :d', d=d)['n']
    assert (k.dau.value, k.dau.previous_value) == (dau(end), dau(end - timedelta(days=28)))

    def week4(d):
        r = q1(wh, """WITH sizes AS (SELECT cohort_week, cohort_size FROM gold.fct_retention_cohorts
                         WHERE weeks_since_signup = 0 AND cohort_week + 6 <= :d
                           AND cohort_week + 34 <= :d)
                      SELECT SUM(COALESCE(r.active_users, 0))::numeric / SUM(s.cohort_size) AS v
                      FROM sizes s LEFT JOIN gold.fct_retention_cohorts r
                        ON r.cohort_week = s.cohort_week AND r.weeks_since_signup = 4""", d=d)
        return r['v']
    assert close(k.week4_retention_rate.value, week4(end))
    assert close(k.week4_retention_rate.previous_value, week4(end - timedelta(days=28)))

    ai = q1(wh, """SELECT COUNT(DISTINCT workspace_id) FILTER (WHERE feature_name IN
                    ('ai_assist', 'ai_voice_agent'))::numeric / COUNT(DISTINCT workspace_id) AS v
                   FROM intermediate.int_feature_usage
                   WHERE DATE_TRUNC('month', event_date)::date = :m""",
            m=k.ai_feature_adoption_rate.period.start)['v']
    assert close(k.ai_feature_adoption_rate.value, ai)

    act = q1(wh, """SELECT AVG(placed_first_call * used_ai_feature * invited_team_member) AS v
                    FROM intermediate.int_activation_funnel
                    WHERE signup_date BETWEEN :s AND :e""",
             s=end - timedelta(days=43), e=end - timedelta(days=14))['v']
    assert close(k.activation_rate_14d.value, act)
    assert k.activation_rate_14d.period.end == end - timedelta(days=14)

    nps = q1(wh, """SELECT ROUND(100.0 * (COUNT(*) FILTER (WHERE score >= 9)
                                 - COUNT(*) FILTER (WHERE score <= 6)) / COUNT(*), 1) AS v
                    FROM staging.stg_nps_responses WHERE response_date > :e - 90 AND response_date <= :e""",
             e=end)['v']
    assert k.nps.value == float(nps)

    res = q1(wh, """SELECT AVG((resolution_path = 'ai_resolved')::int) AS v
                    FROM gold.fct_agent_evaluations WHERE call_date > :e - 30 AND call_date <= :e""",
             e=end)['v']
    assert close(k.ai_resolution_rate.value, res)

    tiers = {r['risk_tier']: r['n'] for r in q(wh, """
        SELECT risk_tier, COUNT(*) AS n FROM analytics.workspace_health_scores
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM analytics.workspace_health_scores)
        GROUP BY 1""")}
    assert {t: v.workspaces for t, v in env.data.health.tiers.items()} == \
        {t: tiers.get(t, 0) for t in env.data.health.tiers}
    decisions = q(wh, "SELECT result_json->>'decision_code' AS c FROM analytics.experiment_results")
    assert env.data.experiments.evaluated == len(decisions)
    assert env.data.experiments.by_decision['SHIP'] == sum(d['c'] == 'SHIP' for d in decisions)


def test_overview_rejects_parameters(wh):
    assert_problem(wh.client.get('/api/overview?start=2025-01-01'), 400, 'invalid-parameter')


# ================================================================ engagement
def test_engagement_summary_and_weeks_equal_raw_event_counts(wh):
    env = get(wh, '/api/engagement', EngagementData)
    s = env.data.summary
    assert s.day == wh.end and env.meta.effective_range.end == wh.end
    # default: 365 days ending at the last loaded day, fitted to the data without a caveat
    assert env.meta.effective_range.start == max(wh.start, wh.end - timedelta(days=364))
    assert not any('clamped' in c for c in env.meta.caveats)
    distinct = q1(wh, 'SELECT COUNT(DISTINCT user_id) AS u, COUNT(DISTINCT workspace_id) AS w '
                      'FROM staging.stg_events WHERE event_date = :d', d=wh.end)
    assert (s.dau, s.active_workspaces) == (distinct['u'], distinct['w'])
    week = next(p for p in env.data.points if p.is_complete)
    avg = q1(wh, """SELECT AVG(n)::float AS v FROM (SELECT event_date, COUNT(DISTINCT user_id) AS n
                    FROM staging.stg_events WHERE event_date BETWEEN :s AND :e GROUP BY 1) d""",
             s=week.period_start, e=week.period_end)['v']
    assert close(week.avg_dau, round(avg, 2), 0.006) and week.days == 7
    assert env.data.points[-1].is_complete is False      # week of Dec 29 ends after Dec 31


def test_engagement_date_filter_boundaries_and_granularity(wh):
    env = get(wh, '/api/engagement', EngagementData, start='2025-03-01', end='2025-03-31',
              granularity='day')
    assert [p.period_start for p in env.data.points][0::30] == [date(2025, 3, 1), date(2025, 3, 31)]
    assert len(env.data.points) == 31 and all(p.is_complete for p in env.data.points)
    assert env.data.summary.day == date(2025, 3, 31)
    early = get(wh, '/api/engagement', EngagementData, start='2024-12-01', end='2025-01-31')
    assert early.meta.effective_range.start == wh.start and any('clamped' in c for c in early.meta.caveats)
    assert early.data.points[0].mau_window_complete is False
    assert_problem(wh.client.get('/api/engagement?granularity=day'), 400, 'invalid-parameter')
    assert_problem(wh.client.get('/api/engagement?granularity=hour'), 422, 'validation-error')
    assert_problem(wh.client.get('/api/engagement?start=2026-02-01'), 400, 'invalid-range')
    assert_problem(wh.client.get('/api/engagement?start=2025-05-01&end=2025-04-01'), 400,
                   'invalid-range')
    assert_problem(wh.client.get('/api/engagement?start=yesterday'), 422, 'validation-error')


# ================================================================ activation
def _funnel_sql(wh, start, end, plan=None):
    return q1(wh, f"""
        SELECT COUNT(*) AS signups, SUM(f.placed_first_call) AS call,
               SUM(f.placed_first_call * f.used_ai_feature) AS call_ai,
               SUM(f.placed_first_call * f.used_ai_feature * f.invited_team_member) AS activated
        FROM intermediate.int_activation_funnel f
        JOIN staging.stg_users u ON u.user_id = f.user_id
        {PLAN_JOIN.format(ws='u.workspace_id', d='f.signup_date')}
        WHERE f.signup_date BETWEEN :s AND :e
          AND (CAST(:p AS text) IS NULL OR COALESCE(s.plan_tier, w.plan_tier) = :p)""",
              s=start, e=end, p=plan)


def test_activation_default_window_is_complete_windows_only(wh):
    env = get(wh, '/api/activation', ActivationData)
    through = wh.end - timedelta(days=14)
    assert env.data.complete_windows_through == through
    assert (env.meta.effective_range.start, env.meta.effective_range.end) == \
        (through - timedelta(days=89), through)
    sql = _funnel_sql(wh, through - timedelta(days=89), through)
    assert [s.users for s in env.data.funnel] == [sql['signups'], sql['call'], sql['call_ai'],
                                                  sql['activated']]
    assert close(env.data.activation_rate_14d, sql['activated'] / sql['signups'])
    assert env.data.excluded_incomplete_signups == 0
    users = [s.users for s in env.data.funnel]
    assert users == sorted(users, reverse=True)                     # funnel ordering
    assert env.data.funnel[0].rate_of_previous is None


def test_activation_plan_filter_and_medians_equal_sql(wh):
    env = get(wh, '/api/activation', ActivationData, start='2025-04-01', end='2025-09-30',
              plan_tier='Enterprise', granularity='month')
    sql = _funnel_sql(wh, date(2025, 4, 1), date(2025, 9, 30), 'Enterprise')
    assert env.data.signups == sql['signups'] and env.data.funnel[3].users == sql['activated']
    assert sum(p.signups for p in env.data.series) == sql['signups'] and len(env.data.series) == 6
    medians = q1(wh, """
        SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY first_call_date - signup_date) AS call,
               percentile_disc(0.75) WITHIN GROUP (ORDER BY first_call_date - signup_date) AS call75
        FROM intermediate.int_activation_funnel
        WHERE placed_first_call = 1 AND signup_date BETWEEN '2025-01-01' AND '2025-12-17'""")
    timing = get(wh, '/api/activation', ActivationData, start='2025-01-01', end='2025-12-17'
                 ).data.time_to_milestone
    call = next(t for t in timing if t.milestone == 'placed_first_call')
    assert (call.median_days, call.p75_days) == (medians['call'], medians['call75'])


def test_activation_incomplete_windows_are_excluded_not_counted_as_failures(wh):
    through = wh.end - timedelta(days=14)
    start = wh.end - timedelta(days=29)
    excluded = q1(wh, 'SELECT COUNT(*) AS n FROM staging.stg_users WHERE signup_date > :t '
                      'AND signup_date <= :e', t=through, e=wh.end)['n']
    env = get(wh, '/api/activation', ActivationData, start=str(start), end=str(wh.end))
    assert env.data.excluded_incomplete_signups == excluded > 0
    assert env.meta.effective_range.end == through and env.meta.caveats
    assert env.data.signups == _funnel_sql(wh, start, through)['signups']
    inc = get(wh, '/api/activation', ActivationData, start=str(start), end=str(wh.end),
              include_incomplete='true')
    assert inc.data.signups == env.data.signups + excluded and inc.data.excluded_incomplete_signups == 0
    # nothing in range has a complete window: an empty, documented response
    empty = get(wh, '/api/activation', ActivationData, start=str(through + timedelta(days=1)))
    assert empty.data.signups == 0 and empty.data.activation_rate_14d is None
    assert empty.meta.effective_range is None and empty.data.series == []
    assert all(t.median_days is None for t in empty.data.time_to_milestone)


def test_activation_parameter_validation(wh):
    c = wh.client
    assert_problem(c.get('/api/activation?plan_tier=Platinum'), 422, 'validation-error')
    assert_problem(c.get('/api/activation?include_incomplete=maybe'), 422, 'validation-error')
    assert_problem(c.get('/api/activation?cohort=1'), 400, 'invalid-parameter')


# ================================================================ retention and cohorts
def _pooled_sql(wh, d):
    return {r['week']: r for r in q(wh, """
        WITH sizes AS (SELECT cohort_week, cohort_size FROM gold.fct_retention_cohorts
                       WHERE weeks_since_signup = 0 AND cohort_week + 6 <= :d),
             weeks AS (SELECT generate_series(0, 12) AS week)
        SELECT w.week, COUNT(s.cohort_week) AS cohorts, COALESCE(SUM(s.cohort_size), 0) AS users,
               COALESCE(SUM(r.active_users), 0) AS active
        FROM weeks w
        LEFT JOIN sizes s ON s.cohort_week + 7 * (w.week + 1) - 1 <= :d
        LEFT JOIN gold.fct_retention_cohorts r
          ON r.cohort_week = s.cohort_week AND r.weeks_since_signup = w.week
        GROUP BY w.week""", d=d)}


@pytest.mark.parametrize('as_of', [None, '2025-06-30'])
def test_retention_pooled_curve_equals_sql(wh, as_of):
    params = {'as_of': as_of} if as_of else {}
    env = get(wh, '/api/retention', RetentionData, **params)
    d = date.fromisoformat(as_of) if as_of else wh.end
    sql = _pooled_sql(wh, d)
    assert env.data.as_of == d
    for p in env.data.pooled_curve:
        s = sql[p.week]
        assert (p.cohorts_included, p.users_included, p.active_users) == \
            (s['cohorts'], s['users'], s['active'])
        assert close(p.retention_rate, s['active'] / s['users'] if s['users'] else None)
    assert env.data.headline.week_4 == env.data.pooled_curve[4].retention_rate
    assert env.data.pooled_curve[0].retention_rate == 1.0


def test_retention_validation(wh):
    c = wh.client
    assert_problem(c.get('/api/retention?as_of=2026-01-01'), 400, 'invalid-range')
    assert_problem(c.get('/api/retention?cohort_start=2025-06-01&cohort_end=2025-05-01'), 400,
                   'invalid-range')
    # 03-05 snaps to Monday 03-03; 03-31 is a Monday: cohorts 03-03, 10, 17, 24, 31
    env = get(wh, '/api/retention', RetentionData, cohort_start='2025-03-05', cohort_end='2025-03-31')
    assert env.data.cohorts == 5 and any('snapped' in c for c in env.meta.caveats)


def test_cohort_matrix_zero_fills_ended_weeks_and_nulls_unfinished_ones(wh):
    env = get(wh, '/api/cohorts', CohortMatrix)
    m = env.data
    assert len(m.cohorts) == 26 and m.weeks == list(range(13))
    cells = {(r['cohort_week'], r['weeks_since_signup']): r for r in q(
        wh, 'SELECT * FROM gold.fct_retention_cohorts')}
    for row in m.cohorts:
        assert row.cohort_week + timedelta(days=6) <= wh.end          # complete first week
        for week, cell in enumerate(row.cells):
            ended = row.cohort_week + timedelta(days=7 * (week + 1) - 1) <= wh.end
            if not ended:
                assert cell is None
                continue
            expected = cells.get((row.cohort_week, week))
            assert cell.active_users == (expected['active_users'] if expected else 0)
            assert close(cell.retention_rate, cell.active_users / row.cohort_size)
    assert m.cohorts[-1].cells[1] is None or m.cohorts[-1].cells[-1] is None
    short = get(wh, '/api/cohorts', CohortMatrix, weeks=4, cohort_start='2025-01-01',
                cohort_end='2025-02-28')
    assert all(len(r.cells) == 5 for r in short.data.cohorts) and len(short.data.cohorts) == 9
    assert_problem(wh.client.get('/api/cohorts?weeks=13'), 422, 'validation-error')


# ================================================================ revenue
def test_revenue_months_equal_workspace_level_sql(wh):
    env = get(wh, '/api/revenue', RevenueData)
    sql = {(r['month_start'], r['plan_tier']): r for r in q(wh, """
        SELECT month_start, plan_tier, SUM(mrr_usd) AS mrr, COUNT(*) FILTER (WHERE mrr_usd > 0) AS paying,
               SUM(billed_seats) AS seats, SUM(mrr_change_usd) AS change
        FROM gold.fct_workspace_mrr GROUP BY 1, 2""")}
    assert len(env.data.series) == 12
    for month in env.data.series:
        rows = [v for (m, _), v in sql.items() if m == month.month]
        assert close(month.mrr_usd, sum(r['mrr'] for r in rows), 0.005)
        assert month.paying_workspaces == sum(r['paying'] for r in rows)
        assert month.billed_seats == sum(r['seats'] for r in rows)
        assert close(month.movement.net_new_usd, sum(r['change'] for r in rows), 0.01)
        assert close(month.movement.net_new_usd, month.mrr_usd - month.previous_mrr_usd, 0.01)
        assert close(sum(p.share_of_mrr or 0 for p in month.by_plan.values()), 1.0, 1e-5) \
            or month.mrr_usd == 0
        for tier, p in month.by_plan.items():
            assert close(p.mrr_usd, sql[(month.month, tier)]['mrr'], 0.005)
    assert env.data.latest == env.data.series[-1] and env.data.latest.is_complete
    assert env.data.latest.movement.contraction_usd <= 0 <= env.data.latest.movement.new_usd


def test_revenue_filters_and_validation(wh):
    env = get(wh, '/api/revenue', RevenueData, plan_tier='Free', group_by='none',
              start_month='2025-06', end_month='2025-08')
    assert [m.month.month for m in env.data.series] == [6, 7, 8]
    for m in env.data.series:      # Free bills nothing: no paying workspaces -> ARPA is null
        assert m.mrr_usd == 0 and m.paying_workspaces == 0 and m.arpa_usd is None
        # previous MRR is that of workspaces that downgraded to Free (null growth if none)
        assert m.mrr_growth_rate == (None if m.previous_mrr_usd == 0 else -1.0)
    assert env.meta.effective_range.end == date(2025, 8, 31)
    c = wh.client
    assert_problem(c.get('/api/revenue?plan_tier=Free'), 400, 'invalid-parameter')
    assert_problem(c.get('/api/revenue?start_month=2025-13'), 422, 'validation-error')
    assert_problem(c.get('/api/revenue?start_month=2026-01'), 400, 'invalid-range')
    assert_problem(c.get('/api/revenue?group_by=workspace'), 422, 'validation-error')


# ================================================================ feature adoption
def test_feature_adoption_equals_sql(wh):
    env = get(wh, '/api/feature-adoption', AdoptionData, features=['ai_assist', 'sms'], max_day=30)
    assert [c.feature for c in env.data.curves] == ['ai_assist', 'sms']
    assert all(len(c.points) == 31 for c in env.data.curves)
    gold = {(r['feature_name'], r['days_since_signup']): r for r in q(
        wh, 'SELECT * FROM gold.fct_feature_adoption')}
    for curve in env.data.curves:
        for p in curve.points:
            g = gold[(curve.feature, p.day)]
            assert close(p.cumulative_rate, float(g['cumulative_adoption_pct']) / 100, 5.1e-5)
            assert close(p.observed_rate, g['eligible_adopters'] / g['eligible_users'])
    at = env.data.adoption_at[0]
    assert at.day_90 is not None and at.day_90.eligible_users < curve.total_users  # censoring
    months = {r['m']: r for r in q(wh, """
        SELECT DATE_TRUNC('month', event_date)::date AS m,
               COUNT(DISTINCT workspace_id) FILTER (WHERE feature_name IN ('ai_assist',
                     'ai_voice_agent'))::numeric / COUNT(DISTINCT workspace_id) AS ai,
               COUNT(DISTINCT workspace_id) FILTER (WHERE feature_name = 'sms') AS sms
        FROM intermediate.int_feature_usage GROUP BY 1""")}
    for m in env.data.monthly:
        assert close(m.ai_adoption_rate, months[m.month]['ai'])
        assert m.by_feature['sms'].active_workspaces == months[m.month]['sms']


def test_feature_adoption_validation(wh):
    c = wh.client
    body = assert_problem(c.get('/api/feature-adoption?features=teleport'), 400, 'invalid-parameter')
    assert 'ai_assist' in body['detail']
    assert_problem(c.get('/api/feature-adoption?' + '&'.join(['features=sms'] * 11)), 422,
                   'validation-error')
    assert_problem(c.get('/api/feature-adoption?max_day=91'), 422, 'validation-error')


# ================================================================ experiments
def test_experiments_list_equals_persisted_results(wh):
    env = get(wh, '/api/experiments', ExperimentList)
    rows = {r['experiment_id']: r for r in q(wh, 'SELECT * FROM analytics.experiment_results')}
    assert {e.experiment.experiment_id for e in env.data.experiments} == set(rows)
    for e in env.data.experiments:
        r = rows[e.experiment.experiment_id]
        assert (e.primary.control_rate, e.primary.treatment_rate, e.primary.p_value) == \
            (r['control_rate'], r['treatment_rate'], r['p_value'])
        assert e.sample == {'control': r['control_users'], 'treatment': r['treatment_users']}
        assert e.decision_text == r['decision'] and e.status == 'completed'
    aa = next(e for e in env.data.experiments if e.experiment.kind == 'aa')
    assert any(aa.experiment.experiment_id in c for c in env.meta.caveats)
    assert get(wh, '/api/experiments', ExperimentList, decision='HOLD').data.experiments == []
    assert get(wh, '/api/experiments', ExperimentList, status='running').data.experiments == []
    assert_problem(wh.client.get('/api/experiments?decision=ship'), 422, 'validation-error')


def test_experiment_detail_equals_result_json_and_curve(wh):
    env = get(wh, '/api/experiments/exp_onboarding_v2', ExperimentDetail)
    ev = env.data.evaluation
    r = q1(wh, "SELECT result_json FROM analytics.experiment_results "
               "WHERE experiment_id = 'exp_onboarding_v2'")['result_json']
    assert ev.primary.ci_lower == r['primary_metric']['ci_lower']
    assert ev.bayesian.prob_treatment_better == r['bayesian_analysis']['prob_treatment_better']
    assert ev.sample_sizes == r['sample_sizes'] and ev.decision_code == r['decision_code']
    assert [g.failed for g in ev.guardrails] == [False, False]      # decision is SHIP
    by_arm = {c.variant: c for c in env.data.activation_curve}
    assert [p.day for p in by_arm['variant_0'].points] == list(range(15))
    assert round(by_arm['variant_0'].points[-1].activation_rate, 4) == ev.primary.control_rate
    assert round(by_arm['variant_1'].points[-1].activation_rate, 4) == ev.primary.treatment_rate


def test_experiment_detail_errors(wh):
    c = wh.client
    body = assert_problem(c.get('/api/experiments/exp_nope'), 404, 'experiment-not-found')
    assert 'exp_onboarding_v2' in body['detail']
    assert_problem(c.get('/api/experiments/EXP-1'), 422, 'validation-error')
    assert_problem(c.get('/api/experiments/' + 'a' * 65), 422, 'validation-error')


# ================================================================ nps
def test_nps_equals_response_level_sql(wh):
    env = get(wh, '/api/nps', NpsData)
    start = wh.end - timedelta(days=89)
    assert env.meta.effective_range.start == start
    sql = q1(wh, """SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE score >= 9) AS p,
                           COUNT(*) FILTER (WHERE score BETWEEN 7 AND 8) AS pa,
                           COUNT(*) FILTER (WHERE score <= 6) AS d
                    FROM staging.stg_nps_responses WHERE response_date BETWEEN :s AND :e""",
             s=start, e=wh.end)
    s = env.data.summary
    assert (s.responses, s.promoters, s.passives, s.detractors) == (sql['n'], sql['p'], sql['pa'], sql['d'])
    assert s.nps == round(100 * (sql['p'] - sql['d']) / sql['n'], 1) and s.margin_of_error_95 > 0
    plans = {r['plan']: r['n'] for r in q(wh, f"""
        SELECT COALESCE(s.plan_tier, w.plan_tier) AS plan, COUNT(*) AS n
        FROM staging.stg_nps_responses n {PLAN_JOIN.format(ws='n.workspace_id', d='n.response_date')}
        WHERE n.response_date BETWEEN :s AND :e GROUP BY 1""", s=start, e=wh.end)}
    assert {p.plan_tier: p.responses for p in env.data.by_plan} == plans
    assert sum(p.responses for p in env.data.series) == s.responses


def test_nps_suppression_and_empty_windows(wh):
    high = get(wh, '/api/nps', NpsData, min_responses=1000).data.summary
    assert high.nps is None and high.suppressed_reason == 'insufficient_responses'
    assert high.promoter_share is not None
    first = q1(wh, 'SELECT MIN(response_date) AS d FROM staging.stg_nps_responses')['d']
    empty = get(wh, '/api/nps', NpsData, start=str(wh.start), end=str(first - timedelta(days=1)),
                granularity='week').data
    assert empty.summary.responses == 0 and empty.summary.nps is None
    assert empty.summary.suppressed_reason == 'no_responses' and empty.summary.promoter_share is None
    assert empty.by_plan == [] and all(p.responses == 0 for p in empty.series)
    assert_problem(wh.client.get('/api/nps?min_responses=5'), 422, 'validation-error')


# ================================================================ support
def test_support_equals_event_and_evaluation_sql(wh):
    env = get(wh, '/api/support', SupportData)
    start = wh.end - timedelta(days=89)
    t = q1(wh, """SELECT COUNT(*) FILTER (WHERE event_name = 'support.ticket_created') AS c,
                         COUNT(*) FILTER (WHERE event_name = 'support.ticket_resolved') AS r
                  FROM staging.stg_events WHERE event_date BETWEEN :s AND :e""", s=start, e=wh.end)
    days = q1(wh, 'SELECT SUM(dau) AS n FROM gold.fct_daily_active_users '
                  'WHERE event_date BETWEEN :s AND :e', s=start, e=wh.end)['n']
    tickets = env.data.tickets
    assert (tickets.created, tickets.resolved, tickets.active_user_days) == (t['c'], t['r'], days)
    assert close(tickets.tickets_per_1k_active_user_days, 1000 * t['c'] / days, 0.0006)
    a = q1(wh, """SELECT COUNT(*) AS n, AVG((resolution_path = 'ai_resolved')::int) AS res,
                         AVG(csat_score)::float AS csat, AVG(handle_time_seconds)::float AS ht
                  FROM gold.fct_agent_evaluations WHERE call_date BETWEEN :s AND :e""",
           s=start, e=wh.end)
    ai = env.data.ai_agent
    assert ai.calls == a['n'] and close(ai.ai_resolution_rate, a['res'])
    assert close(ai.avg_csat, a['csat'], 0.0006) and close(ai.avg_handle_time_seconds, a['ht'], 0.0006)
    assert ai.ai_resolved + ai.escalated + ai.human_handled == ai.calls
    assert sum(c.calls for c in env.data.by_call_type) == ai.calls


def test_support_call_type_filter_empty_window_and_validation(wh):
    inbound = get(wh, '/api/support', SupportData, call_type='inbound').data
    n = q1(wh, "SELECT COUNT(*) AS n FROM gold.fct_agent_evaluations WHERE call_type = 'inbound' "
               "AND call_date > :e - 90 AND call_date <= :e", e=wh.end)['n']
    assert inbound.ai_agent.calls == n and [c.call_type for c in inbound.by_call_type] == ['inbound']
    first = q1(wh, 'SELECT MIN(call_date) AS d FROM gold.fct_agent_evaluations')['d']
    empty = get(wh, '/api/support', SupportData, start=str(wh.start),
                end=str(first - timedelta(days=1)), granularity='day').data
    assert empty.ai_agent.calls == 0 and empty.ai_agent.ai_resolution_rate is None
    assert empty.ai_agent.avg_csat is None and empty.by_call_type == []
    c = wh.client
    assert_problem(c.get('/api/support?call_type=fax'), 400, 'invalid-parameter')
    assert_problem(c.get('/api/support?call_type=In-Bound'), 422, 'validation-error')


# ================================================================ customer health
def test_customer_health_summary_equals_persisted_scores(wh):
    env = get(wh, '/api/customer-health', HealthSummary)
    rows = q(wh, """SELECT risk_tier, health_score, plan_tier FROM analytics.workspace_health_scores
                    WHERE snapshot_date = (SELECT MAX(snapshot_date)
                                           FROM analytics.workspace_health_scores)""")
    d = env.data
    assert d.workspaces == len(rows) and sum(b.workspaces for b in d.histogram) == len(rows)
    assert {t.tier: t.workspaces for t in d.tiers} == \
        {t.tier: sum(r['risk_tier'] == t.tier for r in rows) for t in d.tiers}
    assert close(d.mean_score, round(sum(r['health_score'] for r in rows) / len(rows), 1))
    assert len(d.histogram) == 20 and d.histogram[7].bin_end == 40 and d.histogram[7].tier == 'Critical'
    ent = get(wh, '/api/customer-health', HealthSummary, plan_tier='Enterprise').data
    assert ent.workspaces == sum(r['plan_tier'] == 'Enterprise' for r in rows)
    assert [p.plan_tier for p in ent.by_plan] == ['Enterprise']


def test_workspace_pages_follow_the_allow_listed_sort(wh):
    expected = [r['workspace_id'] for r in q(wh, """
        SELECT workspace_id FROM analytics.workspace_health_scores
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM analytics.workspace_health_scores)
        ORDER BY mrr_usd DESC NULLS LAST, workspace_id""")]
    p1 = get(wh, '/api/customer-health/workspaces', WorkspacePage, sort='mrr_usd', order='desc',
             limit=25).data
    p2 = get(wh, '/api/customer-health/workspaces', WorkspacePage, sort='mrr_usd', order='desc',
             limit=25, offset=25).data
    assert [w.workspace_id for w in p1.items + p2.items] == expected[:50]
    assert p1.total == len(expected) and p1.next_offset == 25
    last = get(wh, '/api/customer-health/workspaces', WorkspacePage, limit=100,
               offset=len(expected) - 10).data
    assert len(last.items) == 10 and last.next_offset is None
    beyond = get(wh, '/api/customer-health/workspaces', WorkspacePage, offset=len(expected) + 5).data
    assert beyond.items == [] and beyond.total == len(expected)
    scores = [w.health_score for w in get(wh, '/api/customer-health/workspaces', WorkspacePage,
                                          tier='Critical', limit=100).data.items]
    assert scores == sorted(scores) and all(s <= 40 for s in scores)
    c = wh.client
    assert_problem(c.get('/api/customer-health/workspaces?sort=workspace_name'), 422,
                   'validation-error')
    assert_problem(c.get('/api/customer-health/workspaces?limit=101'), 422, 'validation-error')
    assert_problem(c.get('/api/customer-health/workspaces?offset=-1'), 422, 'validation-error')
    assert_problem(c.get('/api/customer-health/workspaces?page=2'), 400, 'invalid-parameter')


# ================================================================ security
INJECTIONS = ["' OR 1=1 --", "Free'; DROP TABLE gold.fct_revenue_monthly; --",
              "1); DELETE FROM analytics.workspace_health_scores; --", '%27%20OR%20%271%27=%271',
              'week); SELECT pg_sleep(5); --', "health_score; DROP TABLE x"]


@pytest.mark.parametrize('payload', INJECTIONS)
def test_injection_attempts_are_rejected_without_side_effects(wh, payload):
    c = wh.client
    for path, param in [('/api/activation', 'plan_tier'), ('/api/engagement', 'granularity'),
                        ('/api/engagement', 'start'), ('/api/revenue', 'start_month'),
                        ('/api/feature-adoption', 'features'), ('/api/support', 'call_type'),
                        ('/api/customer-health/workspaces', 'sort'),
                        ('/api/customer-health/workspaces', 'order'),
                        ('/api/experiments', 'decision'), ('/api/nps', 'min_responses')]:
        r = c.get(path, params={param: payload})
        assert r.status_code in (400, 422), (path, param, r.status_code)
    r = c.get('/api/experiments/' + payload.replace('/', ''))
    assert r.status_code in (404, 422)


def test_the_api_never_changed_the_warehouse(wh):
    """Runs last in this module: every request above was read-only."""
    assert fingerprint(wh.owner, schemas=('gold', 'analytics')) == wh.before
