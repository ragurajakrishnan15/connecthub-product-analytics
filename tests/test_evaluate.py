"""Tests for the experiment evaluation pipeline."""
import json

import numpy as np
import pandas as pd

from experimentation.evaluate import evaluate_from_parquet, evaluate_user_metrics


def user_frame(n_per_arm=2000, control_rate=0.20, treatment_rate=0.20,
               control_revenue=30.0, treatment_revenue=30.0, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for variant, rate, revenue in (('variant_0', control_rate, control_revenue),
                                   ('variant_1', treatment_rate, treatment_revenue)):
        rows.append(pd.DataFrame({
            'variant': variant,
            'user_id': [f'{variant}_{i}' for i in range(n_per_arm)],
            'activated_14d': (rng.random(n_per_arm) < rate).astype(int),
            'avg_session_minutes_14d': rng.normal(10, 3, n_per_arm),
            'revenue_60d': rng.normal(revenue, 5, n_per_arm),
            'window_14d_complete': True,
            'window_60d_complete': True,
        }))
    return pd.concat(rows, ignore_index=True)


def test_detects_a_real_lift_and_ships():
    result = evaluate_user_metrics(user_frame(treatment_rate=0.28), 'exp')
    assert result['primary_metric']['significant']
    assert result['primary_metric']['relative_lift'] > 0
    assert result['decision'].startswith('SHIP')


def test_no_difference_is_not_shipped():
    result = evaluate_user_metrics(user_frame(), 'exp')
    assert not result['primary_metric']['significant']
    assert not result['decision'].startswith('SHIP')


def test_revenue_guardrail_blocks_a_winning_variant():
    users = user_frame(treatment_rate=0.28, treatment_revenue=25.0)
    result = evaluate_user_metrics(users, 'exp')
    assert result['guardrail_revenue']['significant']
    assert result['decision'].startswith('REVERT — Revenue guardrail')


def test_session_guardrail_blocks_a_winning_variant():
    users = user_frame(treatment_rate=0.28)
    treated = users['variant'] == 'variant_1'
    users.loc[treated, 'avg_session_minutes_14d'] -= 2
    result = evaluate_user_metrics(users, 'exp')
    assert result['decision'].startswith('REVERT — Session duration guardrail')


def test_incomplete_windows_are_excluded_from_metrics():
    users = user_frame(n_per_arm=1000)
    users.loc[users.index[:300], 'window_14d_complete'] = False
    users.loc[users.index[:500], 'window_60d_complete'] = False
    result = evaluate_user_metrics(users, 'exp')
    sizes = result['sample_sizes']
    assert sizes['control'] == 1000  # SRM uses everyone assigned
    assert sizes['control_14d_window'] == 700
    assert sizes['control_60d_window'] == 500


def test_parquet_evaluation_is_real_and_deterministic(sample_data):
    args = (str(sample_data / 'experiment_assignments.parquet'),
            str(sample_data / 'events.parquet'), 'exp_onboarding_v2')
    first = evaluate_from_parquet(*args)
    second = evaluate_from_parquet(*args)
    assert first == second  # no random simulation left
    json.dumps(first)  # JSON-serializable
    primary = first['primary_metric']
    assert primary['treatment_rate'] > primary['control_rate']
    assert first['guardrail_revenue']['control_mean'] > 0
    assert first['guardrail_session_duration']['control_mean'] > 0
