"""Tests for the behavioral synthetic data generator."""
import pandas as pd
import pytest

import generate_synthetic_data as gen
import ingest_events as ingest
from experimentation.assignment import assign_variant

FILES = ['workspaces', 'users', 'events', 'agent_evaluations', 'subscriptions', 'nps_responses']


def read(data_dir, name, **kw):
    return pd.read_parquet(data_dir / f'{name}.parquet', **kw)


@pytest.fixture(scope='module')
def events(sample_data):
    ev = read(sample_data, 'events')
    ev['event_date'] = pd.to_datetime(ev['event_date'])
    return ev


@pytest.fixture(scope='module')
def users(sample_data):
    return read(sample_data, 'users')


def first_event_dates(events, names):
    return events[events['event_name'].isin(names)].groupby('user_id')['event_date'].min()


def test_same_seed_gives_identical_files(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    gen.generate_dataset(str(a), users=300, workspaces=30, seed=7, verbose=False)
    gen.generate_dataset(str(b), users=300, workspaces=30, seed=7, verbose=False)
    for name in FILES:
        pd.testing.assert_frame_equal(read(a, name), read(b, name))


def test_different_seed_gives_different_ids(tmp_path):
    gen.generate_dataset(str(tmp_path / 'a'), users=50, workspaces=5, seed=1, verbose=False)
    gen.generate_dataset(str(tmp_path / 'b'), users=50, workspaces=5, seed=2, verbose=False)
    ids_a = set(read(tmp_path / 'a', 'users')['user_id'])
    ids_b = set(read(tmp_path / 'b', 'users')['user_id'])
    assert not ids_a & ids_b


def test_rejects_fewer_users_than_workspaces(tmp_path):
    with pytest.raises(ValueError, match='every workspace needs an owner'):
        gen.generate_dataset(str(tmp_path), users=5, workspaces=10, verbose=False)


def test_files_have_the_columns_ingestion_loads(sample_data):
    for spec in ingest.TABLES.values():
        cols = set(read(sample_data, spec['file'].removesuffix('.parquet')).columns)
        assert set(spec['columns']) <= cols, spec['file']


def test_referential_integrity(sample_data, users, events):
    workspace_ids = set(read(sample_data, 'workspaces')['workspace_id'])
    user_ids = set(users['user_id'])
    assert set(users['workspace_id']) <= workspace_ids
    assert set(events['user_id']) <= user_ids
    assert set(events['workspace_id']) <= workspace_ids
    assert set(read(sample_data, 'agent_evaluations')['workspace_id']) <= workspace_ids
    assert set(read(sample_data, 'nps_responses')['user_id']) <= user_ids
    assert set(read(sample_data, 'subscriptions')['workspace_id']) == workspace_ids
    assert events['event_id'].is_unique


def test_events_fall_within_the_year_and_after_signup(users, events):
    assert events['event_date'].min() >= pd.Timestamp('2025-01-01')
    assert events['event_date'].max() <= pd.Timestamp('2025-12-31')
    signup = events['user_id'].map(users.set_index('user_id')['signup_date'])
    assert (events['event_date'] >= signup).all()
    assert (events['timestamp_utc'].dt.normalize() == events['event_date']).all()
    assert set(events['event_name']) <= set(gen.EVENT_TYPES)


def test_first_active_date_is_first_product_use(users, events):
    usage = events[~events['event_name'].isin(gen.ACCOUNT_EVENTS)]
    first_use = usage.groupby('user_id')['event_date'].min()
    expected = users['user_id'].map(first_use)
    pd.testing.assert_series_equal(users['first_active_date'], expected, check_names=False)


def test_activation_funnel_is_ordered(events):
    call = first_event_dates(events, ['call.started'])
    ai = first_event_dates(events, ['ai_assist.used', 'ai_voice_agent.activated'])
    invite = first_event_dates(events, ['team.member_invited'])
    assert set(ai.index) <= set(call.index)
    assert set(invite.index) <= set(ai.index)
    assert (ai >= call.reindex(ai.index)).all()
    assert (invite >= ai.reindex(invite.index)).all()


def activated_14d(users, events):
    signup = events['user_id'].map(users.set_index('user_id')['signup_date'])
    early = events[(events['event_date'] - signup).dt.days <= 14]
    reached = [set(early.loc[early['event_name'].isin(n), 'user_id']) for n in (
        ['call.started'], ['ai_assist.used', 'ai_voice_agent.activated'], ['team.member_invited'])]
    return users['user_id'].isin(set.intersection(*reached))


def test_planted_experiment_effect(users, events):
    treated = users['user_id'].map(lambda u: assign_variant(u, gen.EXPERIMENT_ID)) \
        == gen.TREATMENT_VARIANT
    activated = activated_14d(users, events)
    control_rate = activated[~treated].mean()
    treatment_rate = activated[treated].mean()
    assert 0.10 < control_rate < 0.35
    assert treatment_rate > control_rate * 1.1


def test_activated_users_retain_better(users, events):
    signup = events['user_id'].map(users.set_index('user_id')['signup_date'])
    week = (events['event_date'] - signup).dt.days // 7
    active_week_4 = set(events.loc[week == 4, 'user_id'])
    activated = activated_14d(users, events)
    retained = users['user_id'].isin(active_week_4)
    assert retained[activated].mean() > 2 * retained[~activated].mean()


def test_mrr_is_billed_per_active_seat(sample_data, events):
    subs = read(sample_data, 'subscriptions')
    month = events['event_date'].dt.to_period('M').dt.start_time
    active = events.assign(month_start=month).groupby(['workspace_id', 'month_start'])[
        'user_id'].nunique()
    seats = subs.set_index(['workspace_id', 'month_start'])['billed_seats']
    pd.testing.assert_series_equal(seats, active.reindex(seats.index, fill_value=0),
                                   check_names=False)
    assert (subs['mrr_usd'] == subs['billed_seats'] * subs['seat_price_usd']).all()
    free = subs['plan_tier'] == 'Free'
    assert (subs.loc[free, 'mrr_usd'] == 0).all()
    assert (subs.loc[~free, 'seat_price_usd'] > 0).all()


def test_nps_and_evaluations_are_in_range(sample_data):
    nps = read(sample_data, 'nps_responses')
    assert nps['score'].between(0, 10).all()
    evals = read(sample_data, 'agent_evaluations')
    assert evals['csat_score'].between(1, 5).all()
    assert not (evals['resolved_by_ai'] & evals['escalated_to_human']).any()


def test_sessions_stay_within_a_day(events):
    per_session = events.groupby('session_id').agg(
        users=('user_id', 'nunique'), days=('event_date', 'nunique'))
    assert (per_session['users'] == 1).all()
    assert (per_session['days'] == 1).all()
