"""
Data-quality contracts, per pipeline stage.

Each check is (asset, expectation_type, kwargs, severity). An asset is a
table ("schema.table") or a named SQL query; orphan-key and cross-column
checks are queries that must return zero rows. Severity 'critical' fails the
pipeline; 'warning' is reported only.

Rates stored as percentages in gold (retention_rate, cumulative_adoption_pct)
are validated as fractions in [0, 1] through query assets that divide by 100.
"""
from experimentation.experiments import EXPERIMENTS
from pipeline.config import DATA_END, DATA_START

EVENT_TYPES = [
    'workspace.created', 'user.signup', 'call.started', 'call.ended',
    'call.recorded', 'ai_assist.used', 'ai_assist.summary_generated',
    'ai_voice_agent.activated', 'ai_voice_agent.call_handled',
    'sms.sent', 'sms.received', 'whatsapp.sent', 'whatsapp.received',
    'team.member_invited', 'team.member_joined',
    'billing.plan_upgraded', 'billing.plan_downgraded',
    'support.ticket_created', 'support.ticket_resolved',
    'integration.installed', 'integration.configured',
]
PLAN_TIERS = ['Free', 'Essentials', 'Professional', 'Enterprise']
VARIANTS = ['variant_0', 'variant_1']
RISK_TIERS = ['Critical', 'At Risk', 'Healthy', 'Champion']
CRITICAL, WARNING = 'critical', 'warning'


def _pk(table, *columns):
    """Primary key: not null + no duplicate keys (compound when several columns).

    Uniqueness is a query asset (keys appearing more than once, expected empty)
    rather than GX's expect_column_values_to_be_unique, whose SQL (NOT IN over a
    grouped subquery) took 334 s on 781K events versus ~1 s for this.
    """
    checks = [(table, 'expect_column_values_to_not_be_null', {'column': c}, CRITICAL)
              for c in columns]
    keys = ', '.join(columns)
    name = f"duplicate_keys_{table.replace('.', '_')}"
    QUERIES[name] = f'SELECT {keys} FROM {table} GROUP BY {keys} HAVING COUNT(*) > 1'
    checks.append(_no_rows(name))
    return checks


def _in_set(table, column, values, severity=CRITICAL):
    return (table, 'expect_column_values_to_be_in_set',
            {'column': column, 'value_set': list(values)}, severity)


def _between(table, column, low=None, high=None, severity=CRITICAL):
    kwargs = {'column': column}
    if low is not None:
        kwargs['min_value'] = low
    if high is not None:
        kwargs['max_value'] = high
    return (table, 'expect_column_values_to_be_between', kwargs, severity)


def _no_rows(name, severity=CRITICAL):
    """The named query (see QUERIES) must return no rows."""
    return (name, 'expect_table_row_count_to_equal', {'value': 0}, severity)


def _orphans(child, child_col, parent, parent_col):
    return (f'SELECT c.{child_col} FROM {child} c LEFT JOIN {parent} p '
            f'ON p.{parent_col} = c.{child_col} '
            f'WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL')


QUERIES = {
    # ---- bronze: orphan foreign keys
    'orphan_events_user': _orphans('bronze.events_raw', 'user_id', 'bronze.users_raw', 'user_id'),
    'orphan_events_workspace': _orphans('bronze.events_raw', 'workspace_id',
                                        'bronze.workspaces_raw', 'workspace_id'),
    'orphan_users_workspace': _orphans('bronze.users_raw', 'workspace_id',
                                       'bronze.workspaces_raw', 'workspace_id'),
    'orphan_nps_user': _orphans('bronze.nps_responses', 'user_id', 'bronze.users_raw', 'user_id'),
    'orphan_evals_workspace': _orphans('bronze.agent_evaluations', 'workspace_id',
                                       'bronze.workspaces_raw', 'workspace_id'),
    'orphan_subscriptions_workspace': _orphans('bronze.subscriptions', 'workspace_id',
                                               'bronze.workspaces_raw', 'workspace_id'),
    'orphan_assignments_user': _orphans('experiments.experiment_assignments', 'user_id',
                                        'bronze.users_raw', 'user_id'),
    # ---- bronze: cross-column validity
    'events_before_signup': (
        'SELECT e.event_id FROM bronze.events_raw e JOIN bronze.users_raw u USING (user_id) '
        'WHERE e.event_date < u.signup_date'),
    'events_timestamp_date_mismatch': (
        'SELECT event_id FROM bronze.events_raw WHERE timestamp_utc::date <> event_date'),
    'first_active_before_signup': (
        'SELECT user_id FROM bronze.users_raw WHERE first_active_date < signup_date'),
    'assignment_before_eligibility': (
        "SELECT a.user_id FROM experiments.experiment_assignments a "
        "JOIN bronze.users_raw u USING (user_id) "
        "WHERE a.assigned_at::date < u.signup_date"),
    # ---- gold
    'retention_fraction': ('SELECT retention_rate / 100.0 AS retention_fraction '
                           'FROM gold.fct_retention_cohorts'),
    'adoption_fraction': ('SELECT cumulative_adoption_pct / 100.0 AS adoption_fraction '
                          'FROM gold.fct_feature_adoption'),
    'retention_active_exceeds_cohort': (
        'SELECT cohort_week FROM gold.fct_retention_cohorts WHERE active_users > cohort_size'),
    'dau_wau_mau_order': (
        'SELECT event_date FROM gold.fct_daily_active_users '
        'WHERE NOT (dau <= wau_7d AND wau_7d <= mau_28d)'),
    'orphan_metrics_user': _orphans('gold.fct_experiment_user_metrics', 'user_id',
                                    'staging.stg_users', 'user_id'),
    'orphan_health_workspace': _orphans('gold.metrics_product_health', 'workspace_id',
                                        'staging.stg_workspaces', 'workspace_id'),
    # ---- analytics outputs
    'health_snapshot_incomplete': (
        'SELECT 1 FROM (SELECT COUNT(*) AS n FROM analytics.workspace_health_scores '
        'WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM analytics.workspace_health_scores)'
        ') h, (SELECT COUNT(*) AS n FROM staging.stg_workspaces) w WHERE h.n <> w.n'),
    'experiments_missing_results': (
        'SELECT DISTINCT a.experiment_id FROM experiments.experiment_assignments a '
        'LEFT JOIN analytics.experiment_results r USING (experiment_id) '
        'WHERE r.experiment_id IS NULL'),
    'experiments_with_srm': (
        'SELECT experiment_id FROM analytics.experiment_results WHERE srm_detected'),
}

STAGES = {
    'bronze': [
        *_pk('bronze.events_raw', 'event_id'),
        *[(('bronze.events_raw', 'expect_column_values_to_not_be_null', {'column': c}, CRITICAL))
          for c in ('user_id', 'event_name', 'event_date', 'timestamp_utc')],
        _in_set('bronze.events_raw', 'event_name', EVENT_TYPES),
        _in_set('bronze.events_raw', 'platform', ['web', 'mobile', 'api']),
        _between('bronze.events_raw', 'event_date', DATA_START, DATA_END),
        ('bronze.events_raw', 'expect_table_row_count_to_be_between', {'min_value': 1}, CRITICAL),
        *_pk('bronze.users_raw', 'user_id'),
        _in_set('bronze.users_raw', 'plan_tier', PLAN_TIERS),
        _between('bronze.users_raw', 'signup_date', DATA_START, DATA_END),
        *_pk('bronze.workspaces_raw', 'workspace_id'),
        _in_set('bronze.workspaces_raw', 'plan_tier', PLAN_TIERS),
        _between('bronze.workspaces_raw', 'seat_count', 1),
        _between('bronze.workspaces_raw', 'created_date', DATA_START, DATA_END),
        *_pk('bronze.subscriptions', 'workspace_id', 'month_start'),
        _in_set('bronze.subscriptions', 'plan_tier', PLAN_TIERS),
        _between('bronze.subscriptions', 'mrr_usd', 0),
        _between('bronze.subscriptions', 'seat_price_usd', 0),
        _between('bronze.subscriptions', 'billed_seats', 0),
        _between('bronze.subscriptions', 'month_start', DATA_START, DATA_END),
        *_pk('bronze.nps_responses', 'response_id'),
        _between('bronze.nps_responses', 'score', 0, 10),
        _between('bronze.nps_responses', 'response_date', DATA_START, DATA_END),
        ('bronze.nps_responses', 'expect_table_row_count_to_be_between', {'min_value': 1},
         WARNING),
        *_pk('bronze.agent_evaluations', 'eval_id'),
        _between('bronze.agent_evaluations', 'csat_score', 1, 5),
        _in_set('bronze.agent_evaluations', 'call_type',
                ['inbound', 'outbound', 'internal', 'voicemail']),
        *_pk('experiments.experiment_assignments', 'experiment_id', 'user_id'),
        _in_set('experiments.experiment_assignments', 'variant', VARIANTS),
        _in_set('experiments.experiment_assignments', 'experiment_id', sorted(EXPERIMENTS)),
        *[_no_rows(q) for q in ('orphan_events_user', 'orphan_events_workspace',
                                'orphan_users_workspace', 'orphan_nps_user',
                                'orphan_evals_workspace', 'orphan_subscriptions_workspace',
                                'orphan_assignments_user', 'events_before_signup',
                                'events_timestamp_date_mismatch', 'first_active_before_signup',
                                'assignment_before_eligibility')],
    ],
    'gold': [
        _between('retention_fraction', 'retention_fraction', 0, 1),
        _between('adoption_fraction', 'adoption_fraction', 0, 1),
        _no_rows('retention_active_exceeds_cohort'),
        *_pk('gold.fct_daily_active_users', 'event_date'),
        _no_rows('dau_wau_mau_order'),
        *_pk('gold.fct_experiment_user_metrics', 'experiment_id', 'user_id'),
        _in_set('gold.fct_experiment_user_metrics', 'activated_14d', [0, 1]),
        _in_set('gold.fct_experiment_user_metrics', 'variant', VARIANTS),
        _between('gold.fct_experiment_user_metrics', 'revenue_60d', 0),
        *_pk('gold.fct_workspace_mrr', 'workspace_id', 'month_start'),
        _between('gold.fct_workspace_mrr', 'mrr_usd', 0),
        *_pk('gold.metrics_product_health', 'workspace_id'),
        _between('gold.metrics_product_health', 'pct_ai_calls_automated', 0, 1),
        _between('gold.metrics_product_health', 'nps_score', -100, 100),
        _between('gold.metrics_product_health', 'mrr_usd', 0),
        _between('gold.metrics_product_health', 'dau_over_seats_ratio', 0),
        _no_rows('orphan_metrics_user'),
        _no_rows('orphan_health_workspace'),
    ],
    'analytics': [
        *_pk('analytics.workspace_health_scores', 'snapshot_date', 'workspace_id'),
        ('analytics.workspace_health_scores', 'expect_column_values_to_not_be_null',
         {'column': 'health_score'}, CRITICAL),
        _between('analytics.workspace_health_scores', 'health_score', 0, 100),
        _in_set('analytics.workspace_health_scores', 'risk_tier', RISK_TIERS),
        _no_rows('health_snapshot_incomplete'),
        *_pk('analytics.experiment_results', 'experiment_id'),
        _between('analytics.experiment_results', 'p_value', 0, 1),
        _between('analytics.experiment_results', 'srm_p_value', 0, 1),
        _no_rows('experiments_missing_results'),
        # A sample ratio mismatch means the assignment or the data is broken.
        _no_rows('experiments_with_srm'),
    ],
}
