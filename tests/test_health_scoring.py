"""Tests for customer health scoring."""
import numpy as np
import pandas as pd

from analytics.health_scoring import (
    INPUT_COLUMNS, TIER_LABELS, _score, compute_health_from_parquet, health_inputs_from_parquet,
)


def workspaces(n=5, **overrides):
    """n workspaces whose inputs all improve with the index."""
    i = np.arange(n, dtype=float)
    df = pd.DataFrame({
        'workspace_id': [f'w{k}' for k in range(n)],
        'active_users_30d': 1 + i,
        'dau_over_seats_ratio': 0.1 * (1 + i),
        'features_adopted_count': 1 + i,
        'pct_ai_calls_automated': 0.1 * i,
        'avg_session_duration_minutes': 5 + i,
        'nps_score': -50 + 25 * i,
        'mrr_usd': 100 + 10 * i,
        'mrr_change_usd': i - 2,
        'support_tickets_last_30d': (n - i),  # fewer tickets as health improves
    })
    for col, values in overrides.items():
        df[col] = values
    return df


def scores(df):
    return _score(df).set_index('workspace_id')['health_score']


def test_scores_are_0_to_100_and_follow_the_inputs():
    s = scores(workspaces())
    assert s.between(0, 100).all()
    assert s.loc['w4'] == 100.0
    assert list(s.sort_index()) == sorted(s)


def test_fewer_tickets_per_active_user_is_better():
    df = workspaces(2, dau_over_seats_ratio=[0.5, 0.5], features_adopted_count=[3, 3],
                    pct_ai_calls_automated=[0.2, 0.2], avg_session_duration_minutes=[9, 9],
                    nps_score=[10, 10], mrr_change_usd=[0, 0],
                    active_users_30d=[10, 10], support_tickets_last_30d=[1, 8])
    s = scores(df)
    assert s.loc['w0'] > s.loc['w1']


def test_missing_signal_is_skipped_not_counted_as_zero():
    df = workspaces(3)
    with_nps = scores(df)
    df.loc[df['workspace_id'] == 'w2', 'nps_score'] = np.nan
    without = scores(df)
    assert without.loc['w2'] == 100.0  # still best on every remaining input
    assert with_nps.loc['w2'] == 100.0


def test_inactive_workspace_is_not_rewarded_for_having_no_tickets():
    df = workspaces(3)
    df.loc[0, ['active_users_30d', 'dau_over_seats_ratio', 'features_adopted_count',
               'avg_session_duration_minutes', 'support_tickets_last_30d']] = 0
    s = scores(df)
    assert s.loc['w0'] == s.min()
    assert s.loc['w0'] < 40


def test_free_plan_has_no_revenue_signal():
    df = workspaces(2, mrr_usd=[0.0, 0.0], mrr_change_usd=[0.0, 0.0])
    a = scores(df)
    b = scores(df.drop(columns=['mrr_usd', 'mrr_change_usd']))
    pd.testing.assert_series_equal(a, b)


def test_returns_raw_inputs_with_tier():
    df = workspaces()
    out = _score(df).set_index('workspace_id').sort_index()
    pd.testing.assert_series_equal(out['mrr_change_usd'], df.set_index('workspace_id')[
        'mrr_change_usd'])
    assert set(out['risk_tier'].astype(str)) <= set(TIER_LABELS)


def test_parquet_inputs_one_row_per_workspace(sample_data):
    inputs = health_inputs_from_parquet(str(sample_data))
    ws = pd.read_parquet(sample_data / 'workspaces.parquet')
    assert list(inputs.columns) == INPUT_COLUMNS
    assert sorted(inputs['workspace_id']) == sorted(ws['workspace_id'])
    required = [c for c in INPUT_COLUMNS if c not in ('pct_ai_calls_automated', 'nps_score')]
    assert not inputs[required].isna().any().any()
    assert inputs['pct_ai_calls_automated'].dropna().between(0, 1).all()
    assert inputs['nps_score'].dropna().between(-100, 100).all()


def test_backtest_as_of_ignores_later_data(sample_data):
    early = health_inputs_from_parquet(str(sample_data), as_of='2025-06-30')
    assert (early['snapshot_date'].astype(str) == '2025-06-30').all()
    scored = compute_health_from_parquet(
        str(sample_data / 'users.parquet'), str(sample_data / 'events.parquet'),
        str(sample_data / 'workspaces.parquet'), as_of='2025-06-30')
    assert scored['health_score'].dropna().between(0, 100).all()
