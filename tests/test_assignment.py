"""Tests for the experiment assignment engine and registry."""
import os
import uuid

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from experimentation import assignment
from experimentation.assignment import assign_users, assign_variant, persist_assignments
from experimentation.experiments import EXPERIMENTS, Experiment, get_experiment
from experimentation.stat_tests import srm_check


class TestAssignVariant:
    def test_deterministic(self):
        v1 = assign_variant('user_123', 'exp_001')
        v2 = assign_variant('user_123', 'exp_001')
        assert v1 == v2

    def test_different_users_get_different_variants(self):
        variants = {assign_variant(f'user_{i}', 'exp_001') for i in range(100)}
        assert len(variants) == 2  # variant_0 and variant_1

    def test_different_experiments_different_assignment(self):
        v1 = assign_variant('user_123', 'exp_001')
        v2 = assign_variant('user_123', 'exp_002')
        # Not guaranteed to differ, but tests the mechanism works
        assert v1 in ('variant_0', 'variant_1')
        assert v2 in ('variant_0', 'variant_1')

    def test_holdout(self):
        holdout = sum(assign_variant(f'user_{i}', 'exp_001', traffic_pct=0.5) == 'holdout'
                      for i in range(10000))
        # Should be roughly 50% holdout
        assert 4000 < holdout < 6000

    def test_multi_variant(self):
        variants = {assign_variant(f'user_{i}', 'exp_001', num_variants=4) for i in range(1000)}
        assert len(variants) == 4

    def test_balance(self):
        counts = pd.Series([assign_variant(f'user_{i}', 'exp_balance_test')
                            for i in range(10000)]).value_counts()
        ratio = counts['variant_0'] / counts['variant_1']
        assert 0.9 < ratio < 1.1  # within 10% balance

    def test_balance_on_large_sample_passes_srm(self):
        ids = [str(uuid.UUID(int=i)) for i in range(100_000)]
        counts = pd.Series([assign_variant(u, 'exp_large') for u in ids]).value_counts()
        assert srm_check(int(counts['variant_0']), int(counts['variant_1']))['p_value'] > 0.001
        assert abs(counts['variant_0'] / 100_000 - 0.5) < 0.01

    def test_traffic_and_variant_are_independent(self):
        """In-traffic users split 50/50 regardless of their traffic bucket."""
        ids = [f'u{i}' for i in range(40_000)]
        buckets = np.array([assignment.traffic_bucket(u, 'exp_x') for u in ids])
        variants = np.array([assign_variant(u, 'exp_x', traffic_pct=0.5) for u in ids])
        inside = variants != 'holdout'
        assert (inside == (buckets < 5000)).all()
        low = inside & (buckets < 2500)
        high = inside & (buckets >= 2500)
        for part in (low, high):
            share = (variants[part] == 'variant_1').mean()
            assert 0.47 < share < 0.53

    def test_salt_reshuffles_arms_but_not_traffic(self):
        ids = [f'u{i}' for i in range(5000)]
        a = [assign_variant(u, 'exp_s', traffic_pct=0.3, salt='a') for u in ids]
        b = [assign_variant(u, 'exp_s', traffic_pct=0.3, salt='b') for u in ids]
        assert [x == 'holdout' for x in a] == [x == 'holdout' for x in b]
        changed = np.mean([x != y for x, y in zip(a, b) if x != 'holdout'])
        assert 0.4 < changed < 0.6


def users_frame():
    return pd.DataFrame({
        'user_id': ['early', 'in1', 'in2', 'late'],
        'signup_date': pd.to_datetime(['2025-01-31', '2025-02-01', '2025-02-28', '2025-03-01']),
    })


class TestEligibility:
    def test_new_signups_window_and_assigned_at_signup(self):
        exp = Experiment('exp_window', start='2025-02-01', end='2025-02-28',
                         eligibility='new_signups')
        out = assign_users(exp, users_frame())
        assert sorted(out['user_id']) == ['in1', 'in2']
        signup = users_frame().set_index('user_id')['signup_date'].dt.tz_localize('UTC')
        assert (out.set_index('user_id')['assigned_at'] == signup.loc[out['user_id']]).all()

    def test_active_users_enter_at_first_activity_after_start(self, tmp_path):
        events = pd.DataFrame({
            'user_id': ['early', 'early', 'in1', 'in1', 'late'],
            'timestamp_utc': pd.to_datetime([
                '2025-01-15 09:00', '2025-02-03 10:00',  # first activity *after* start
                '2025-02-01 08:00', '2025-02-01 07:00',  # earliest of two on the start day
                '2025-03-05 12:00',                       # after the window: not eligible
            ]),
        })
        path = tmp_path / 'events.parquet'
        events.to_parquet(path, index=False)
        exp = Experiment('exp_active', start='2025-02-01', end='2025-02-28',
                         eligibility='active_users')
        out = assign_users(exp, users_frame(), str(path)).set_index('user_id')
        assert sorted(out.index) == ['early', 'in1']
        assert out.loc['early', 'assigned_at'] == pd.Timestamp('2025-02-03 10:00', tz='UTC')
        assert out.loc['in1', 'assigned_at'] == pd.Timestamp('2025-02-01 07:00', tz='UTC')

    def test_traffic_allocation_excludes_holdout(self):
        users = pd.DataFrame({'user_id': [f'u{i}' for i in range(4000)],
                              'signup_date': pd.Timestamp('2025-06-01')})
        exp = Experiment('exp_half', '2025-01-01', '2025-12-31', 'new_signups', traffic_pct=0.5)
        out = assign_users(exp, users)
        assert 1800 < len(out) < 2200
        assert set(out['variant']) == {'variant_0', 'variant_1'}

    def test_unknown_experiment_is_a_clear_error(self):
        with pytest.raises(KeyError, match='known: '):
            get_experiment('exp_nope')


class TestOnGeneratedData:
    def test_deterministic_and_unique(self, sample_data):
        users = pd.read_parquet(sample_data / 'users.parquet')
        events = str(sample_data / 'events.parquet')
        for exp in EXPERIMENTS.values():
            a = assign_users(exp, users, events)
            b = assign_users(exp, users, events)
            pd.testing.assert_frame_equal(a, b)
            assert a['user_id'].is_unique

    def test_arms_balanced_and_experiments_independent(self, sample_data):
        users = pd.read_parquet(sample_data / 'users.parquet')
        events = str(sample_data / 'events.parquet')
        frames = {e: assign_users(x, users, events).set_index('user_id')['variant']
                  for e, x in EXPERIMENTS.items()}
        for variants in frames.values():
            counts = variants.value_counts()
            assert srm_check(int(counts['variant_0']), int(counts['variant_1']))['p_value'] > 0.001
        both = pd.concat(frames, axis=1, join='inner')
        table = pd.crosstab(both.iloc[:, 0], both.iloc[:, 1]).to_numpy()
        from scipy.stats import chi2_contingency
        assert chi2_contingency(table).pvalue > 0.001

    def test_assigned_at_respects_the_experiment_window(self, sample_data):
        users = pd.read_parquet(sample_data / 'users.parquet').set_index('user_id')
        for exp in EXPERIMENTS.values():
            out = assign_users(exp, users.reset_index(), str(sample_data / 'events.parquet'))
            at = out['assigned_at'].dt.tz_localize(None)
            assert (at >= exp.start_ts).all() and (at <= exp.end_ts).all()
            signup = users.loc[out['user_id'], 'signup_date'].to_numpy()
            assert (at.dt.normalize().to_numpy() >= signup).all()

    def test_aa_false_positive_rate_is_calibrated(self, sample_data):
        """Re-randomizing the A/A population many times, ~5% of runs are 'significant'."""
        from experimentation.evaluate import user_metrics_from_parquet
        from experimentation.stat_tests import z_test_proportions
        metrics = user_metrics_from_parquet(
            sample_data / 'experiment_assignments.parquet', sample_data / 'events.parquet',
            sample_data / 'users.parquet', sample_data / 'subscriptions.parquet',
            'exp_onboarding_v2')
        m = metrics[metrics['window_14d_complete']]
        y = m['activated_14d'].to_numpy()
        significant = 0
        for k in range(200):
            t = np.array([assign_variant(u, 'exp_aa', salt=f's{k}') == 'variant_1'
                          for u in m['user_id']])
            r = z_test_proportions(int(y[~t].sum()), int((~t).sum()), int(y[t].sum()),
                                   int(t.sum()))
            significant += r['p_value'] < 0.05
        assert 0.01 <= significant / 200 <= 0.10


@pytest.fixture
def scratch_table():
    if not os.environ.get('POSTGRES_PASSWORD'):
        pytest.skip('POSTGRES_PASSWORD not set; no PostgreSQL configured')
    from pipeline.config import create_engine
    engine = create_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f'PostgreSQL not reachable: {exc}')
    schema = f'test_assign_{uuid.uuid4().hex[:8]}'
    yield engine, f'{schema}.experiment_assignments'
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
    engine.dispose()


@pytest.mark.integration
def test_persistence_is_idempotent(scratch_table, sample_data):
    engine, table = scratch_table
    users = pd.read_parquet(sample_data / 'users.parquet')
    out = pd.concat([assign_users(x, users, str(sample_data / 'events.parquet'))
                     for x in EXPERIMENTS.values()], ignore_index=True)
    assert persist_assignments(engine, out, table) == len(out)
    assert persist_assignments(engine, out, table) == 0  # rerun: nothing new
    # A conflicting re-assignment never overwrites the first one.
    flipped = out.head(10).assign(variant=lambda d: d['variant'].map(
        {'variant_0': 'variant_1', 'variant_1': 'variant_0'}))
    assert persist_assignments(engine, flipped, table) == 0
    with engine.connect() as conn:
        n, dupes = conn.execute(text(
            f'SELECT COUNT(*), COUNT(*) - COUNT(DISTINCT (experiment_id, user_id)) FROM {table}'
        )).one()
        stored = pd.read_sql(text(f'SELECT experiment_id, user_id, variant FROM {table}'), conn)
    assert n == len(out) and dupes == 0
    key = ['experiment_id', 'user_id']
    kept = out.head(10).merge(stored, on=key, suffixes=('_first', '_stored'))
    assert len(kept) == 10 and (kept['variant_first'] == kept['variant_stored']).all()
