"""
Full Experiment Evaluation Pipeline
End-to-end evaluation with primary metrics, Bayesian analysis, and guardrails.
"""
import pandas as pd
import numpy as np
from experimentation.stat_tests import z_test_proportions, t_test_continuous, srm_check
from experimentation.bayesian_ab import bayesian_ab_test


def evaluate_experiment(conn, experiment_id, metric='activation_14d'):
    """End-to-end experiment evaluation with guardrail checks.

    Conversion = fully activated within 14 days (call + AI feature + team invite).
    """
    query = """
    WITH exp_users AS (
        SELECT
            ea.variant,
            ea.user_id,
            CASE WHEN af.placed_first_call = 1
                  AND af.used_ai_feature = 1
                  AND af.invited_team_member = 1
                 THEN 1 ELSE 0 END AS activated_14d
        FROM gold.fct_experiment_assignments ea
        JOIN intermediate.int_activation_funnel af ON af.user_id = ea.user_id
        WHERE ea.experiment_id = %(exp_id)s
    ),
    user_sessions AS (
        SELECT user_id, AVG(session_duration_min) AS avg_session_min
        FROM intermediate.int_sessions
        GROUP BY user_id
    )
    SELECT
        u.variant,
        COUNT(*) AS total_users,
        SUM(u.activated_14d) AS conversions,
        AVG(s.avg_session_min) AS avg_session_duration,
        NULL::NUMERIC AS avg_revenue  -- no revenue data in the warehouse yet
    FROM exp_users u
    LEFT JOIN user_sessions s ON s.user_id = u.user_id
    GROUP BY u.variant
    """
    df = pd.read_sql(query, conn, params={'exp_id': experiment_id})
    return _evaluate(df, experiment_id)


def evaluate_from_parquet(assignments_path, events_path, experiment_id):
    """Evaluate experiment from local parquet files."""
    assignments = pd.read_parquet(assignments_path)
    # NOTE: metrics below are still SIMULATED; events_path is not read yet.
    # Replacing this with real per-user metrics is planned for Phase 2.

    exp_users = assignments[assignments['experiment_id'] == experiment_id]

    # Simulate metrics per variant
    results = []
    for variant in exp_users['variant'].unique():
        variant_users = exp_users[exp_users['variant'] == variant]
        n = len(variant_users)

        # Simulate conversion rates (treatment slightly higher)
        base_rate = 0.32
        if variant == 'variant_1':
            base_rate = 0.34  # simulated 6.25% lift

        conversions = int(np.random.binomial(n, base_rate))
        results.append({
            'variant': variant,
            'total_users': n,
            'conversions': conversions,
            'avg_session_duration': round(np.random.normal(12, 2), 2),
            'avg_revenue': round(np.random.normal(45, 8), 2)
        })

    df = pd.DataFrame(results)
    return _evaluate(df, experiment_id)


def _evaluate(df, experiment_id):
    """Core evaluation logic."""
    control = df[df['variant'] == 'variant_0'].iloc[0]
    treatment = df[df['variant'] == 'variant_1'].iloc[0]

    # SRM check
    srm = srm_check(
        int(control['total_users']),
        int(treatment['total_users'])
    )

    # Primary metric: conversion rate (frequentist)
    primary = z_test_proportions(
        int(control['conversions']), int(control['total_users']),
        int(treatment['conversions']), int(treatment['total_users'])
    )

    # Bayesian complement
    bayesian = bayesian_ab_test(
        int(control['conversions']), int(control['total_users']),
        int(treatment['conversions']), int(treatment['total_users'])
    )

    # Guardrail: revenue per user (must not decrease significantly)
    # Using simulated individual values for t-test. Skipped (None) when the
    # source has no revenue data, e.g. the current warehouse.
    guardrail_revenue = None
    if pd.notna(control['avg_revenue']) and pd.notna(treatment['avg_revenue']):
        np.random.seed(42)
        control_rev = np.random.normal(control['avg_revenue'], 8, int(control['total_users']))
        treatment_rev = np.random.normal(treatment['avg_revenue'], 8, int(treatment['total_users']))
        guardrail_revenue = t_test_continuous(control_rev, treatment_rev)

    return {
        'experiment_id': experiment_id,
        'srm_check': srm,
        'primary_metric': primary,
        'bayesian_analysis': bayesian,
        'guardrail_revenue': guardrail_revenue,
        'sample_sizes': {
            'control': int(control['total_users']),
            'treatment': int(treatment['total_users'])
        },
        'decision': _make_decision(primary, bayesian, srm, guardrail_revenue)
    }


def _make_decision(primary, bayesian, srm, guardrail):
    """Automated decision recommendation."""
    if srm['srm_detected']:
        return 'HOLD — Sample ratio mismatch detected, investigate assignment logic'
    if guardrail is not None and guardrail['significant'] and guardrail['absolute_diff'] < 0:
        return 'REVERT — Revenue guardrail failed (significant decrease)'
    if primary['significant'] and primary['relative_lift'] > 0:
        return f"SHIP — Significant lift of {primary['relative_lift']:.1%} (p={primary['p_value']:.4f})"
    if bayesian['prob_treatment_better'] > 0.95:
        return f"SHIP — Bayesian probability {bayesian['prob_treatment_better']:.1%} treatment is better"
    if bayesian['prob_treatment_better'] > 0.80:
        return 'CONTINUE — Promising but needs more data'
    return 'REVERT — No significant improvement detected'


if __name__ == '__main__':
    import json
    result = evaluate_from_parquet(
        'data/experiment_assignments.parquet',
        'data/events.parquet',
        'exp_onboarding_v2'
    )
    print(json.dumps(result, indent=2, default=str))
