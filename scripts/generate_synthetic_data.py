"""
ConnectHub Synthetic Data Generator

Produces a year (2025) of product data with real behavioral structure, so the
analytics built on top of it have signal to find:

- Each workspace has a latent engagement level that drives its users' behavior,
  its plan changes, support load, AI voice agent quality and NPS.
- Users move through an ordered 14-day activation funnel:
  first call -> first AI feature use -> first team invite.
- Daily activity decays with days since signup; activated users churn later.
- Revenue is per active seat: each month a workspace pays its plan's seat price
  for every user with activity that month (bronze.subscriptions).
- NPS survey responses at days 30/120/210/300, driven by engagement.
- Agent evaluations are generated from ai_voice_agent.call_handled events,
  so every evaluation belongs to a workspace.
- Experiment exp_onboarding_v2 has a planted effect: users that
  experimentation.assignment puts in variant_1 are EXPERIMENT_AI_LIFT times as
  likely to use an AI feature after their first call. Nothing else differs.

Everything is drawn from one seeded generator and IDs are derived from the seed,
so the same arguments always produce identical files. Events are generated and
written in chunks of users, which bounds memory regardless of --users.

Outputs (data/): workspaces, users, events, agent_evaluations, subscriptions,
nps_responses (all .parquet).
"""
import argparse
import os
import sys
import uuid

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experimentation.assignment import assign_variant  # noqa: E402

NUM_USERS = 10_000
START_DATE = np.datetime64('2025-01-01')
DAYS = 365
NEVER = 1_000_000  # sentinel "day" for milestones that never happen
CHUNK_USERS = 20_000

EVENT_TYPES = [
    'workspace.created', 'user.signup', 'call.started', 'call.ended',
    'call.recorded', 'ai_assist.used', 'ai_assist.summary_generated',
    'ai_voice_agent.activated', 'ai_voice_agent.call_handled',
    'sms.sent', 'sms.received', 'whatsapp.sent', 'whatsapp.received',
    'team.member_invited', 'team.member_joined',
    'billing.plan_upgraded', 'billing.plan_downgraded',
    'support.ticket_created', 'support.ticket_resolved',
    'integration.installed', 'integration.configured'
]
EV = {name: i for i, name in enumerate(EVENT_TYPES)}

PLAN_TIERS = ['Free', 'Essentials', 'Professional', 'Enterprise']
PLAN_PROBS = [0.40, 0.30, 0.20, 0.10]
SEAT_PRICE_USD = np.array([0.0, 15.0, 25.0, 45.0])  # per active seat per month
PLAN_ENGAGEMENT_SHIFT = np.array([-0.2, 0.0, 0.15, 0.3])
SEAT_OPTIONS = [1, 3, 5, 10, 15, 25, 50, 100]
SEAT_PROBS = [0.10, 0.15, 0.20, 0.20, 0.15, 0.10, 0.07, 0.03]
PLATFORMS = ['web', 'mobile', 'api']
PLATFORM_PROBS = [0.55, 0.35, 0.10]
CALL_TYPES = ['inbound', 'outbound', 'internal', 'voicemail']
CALL_TYPE_PROBS = [0.55, 0.25, 0.10, 0.10]
COUNTRIES = ['US', 'FR', 'DE', 'GB', 'ES', 'AU', 'MX', 'BR']
COUNTRY_PROBS = [0.30, 0.15, 0.12, 0.10, 0.08, 0.08, 0.10, 0.07]

EXPERIMENT_ID = 'exp_onboarding_v2'
TREATMENT_VARIANT = 'variant_1'
EXPERIMENT_AI_LIFT = 1.30
NPS_SURVEY_DAYS = (30, 120, 210, 300)
NPS_RESPONSE_RATE = 0.35

# Day index (0..DAYS-1) -> month index (0..11)
DAY_MONTH = ((START_DATE + np.arange(DAYS)).astype('datetime64[M]')
             - START_DATE.astype('datetime64[M]')).astype(int)
N_MONTHS = int(DAY_MONTH[-1]) + 1


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _stable_ids(seed, kind, n):
    """IDs that depend only on (seed, kind, index): same input, same IDs."""
    ns = uuid.uuid5(uuid.NAMESPACE_DNS, f'connecthub.synthetic.{seed}')
    return np.array([str(uuid.uuid5(ns, f'{kind}:{i}')) for i in range(n)], dtype=object)


_HEX = np.array(list('0123456789abcdef'))


def _random_uuids(rng, n):
    """Vectorized UUID4-format strings drawn from rng (deterministic per seed)."""
    if n == 0:
        return np.array([], dtype=object)
    b = np.frombuffer(rng.bytes(16 * n), dtype=np.uint8).reshape(n, 16).copy()
    b[:, 6] = (b[:, 6] & 0x0F) | 0x40
    b[:, 8] = (b[:, 8] & 0x3F) | 0x80
    digits = np.empty((n, 32), dtype='<U1')
    digits[:, 0::2] = _HEX[b >> 4]
    digits[:, 1::2] = _HEX[b & 0x0F]
    dash = np.full((n, 1), '-', dtype='<U1')
    parts = np.concatenate([digits[:, :8], dash, digits[:, 8:12], dash, digits[:, 12:16],
                            dash, digits[:, 16:20], dash, digits[:, 20:]], axis=1)
    return np.ascontiguousarray(parts).view('<U36').ravel().astype(object)


def _geometric_day(rng, p, n):
    """Days until an event with daily probability p (0 = same day)."""
    return rng.geometric(p, n) - 1


def generate_workspaces(n, rng, seed):
    """Workspace dimension plus the latent state that drives behavior."""
    # Signups grow over the year; every workspace has at least 30 days of history.
    created_day = ((DAYS - 30) * rng.random(n) ** 0.8).astype(int)
    plan_initial = rng.choice(len(PLAN_TIERS), n, p=PLAN_PROBS)
    engagement = rng.normal(0, 1, n) + PLAN_ENGAGEMENT_SHIFT[plan_initial]

    # At most one plan change per workspace; engaged workspaces upgrade.
    up = (plan_initial < 3) & (rng.random(n) < 0.5 * _sigmoid(1.5 * engagement - 1.0))
    down = ~up & (plan_initial > 0) & (rng.random(n) < 0.5 * _sigmoid(-1.5 * engagement - 1.5))
    change_day = created_day + rng.integers(30, 240, n)
    changed = (up | down) & (change_day < DAYS)
    plan_final = plan_initial + (up & changed) - (down & changed)

    return {
        'id': _stable_ids(seed, 'workspace', n),
        'created_day': created_day,
        'plan_initial': plan_initial,
        'plan_final': plan_final,
        'change_day': np.where(changed, change_day, NEVER),
        'upgraded': up & changed,
        'engagement': engagement,
        'seats': rng.choice(SEAT_OPTIONS, n, p=SEAT_PROBS),
        'country': rng.choice(COUNTRIES, n, p=COUNTRY_PROBS),
        'voice_agent': rng.random(n) < _sigmoid(-1.0 + engagement),
        'ticket_rate': 0.03 * np.exp(-0.8 * engagement),
    }


def generate_users(n, ws, rng, seed):
    """User dimension plus per-user behavior parameters (funnel, retention)."""
    n_ws = len(ws['id'])
    if n < n_ws:
        raise ValueError(f'--users ({n}) must be >= --workspaces ({n_ws}): '
                         'every workspace needs an owner')
    # The first n_ws users own one workspace each; the rest join by seat count.
    ws_idx = np.concatenate([
        np.arange(n_ws),
        rng.choice(n_ws, n - n_ws, p=ws['seats'] / ws['seats'].sum()),
    ])
    owner = np.arange(n) < n_ws
    created = ws['created_day'][ws_idx]
    signup = created + rng.exponential(45, n).astype(int)
    late = signup >= DAYS
    signup[late] = rng.integers(created[late], DAYS)
    signup[owner] = created[owner]

    ids = _stable_ids(seed, 'user', n)
    engagement = 0.7 * ws['engagement'][ws_idx] + rng.normal(0, 0.7, n)
    treated = np.array([assign_variant(u, EXPERIMENT_ID) == TREATMENT_VARIANT for u in ids])

    # Ordered activation funnel. Milestone days are counted from signup.
    called = rng.random(n) < _sigmoid(1.0 + 0.8 * engagement)
    call_day = np.where(called, _geometric_day(rng, 0.35, n), NEVER)
    p_ai = np.clip(_sigmoid(-0.4 + 0.8 * engagement)
                   * np.where(treated, EXPERIMENT_AI_LIFT, 1.0), 0, 0.95)
    used_ai = called & (rng.random(n) < p_ai)
    ai_day = np.where(used_ai, call_day + _geometric_day(rng, 0.3, n), NEVER)
    invited = used_ai & (rng.random(n) < _sigmoid(0.3 + 0.8 * engagement))
    invite_day = np.where(invited, ai_day + _geometric_day(rng, 0.3, n), NEVER)
    # Some callers find the AI features later, outside the onboarding window.
    late_ai = called & ~used_ai & (rng.random(n) < 0.2)
    ai_day = np.where(late_ai, np.maximum(call_day + 1, 14 + rng.integers(0, 90, n)), ai_day)

    ai_14 = ai_day <= 13
    full_14 = invite_day <= 13
    # Days until churn. Some users are retained long term (beyond the data);
    # activation makes both outcomes better.
    lifetime = rng.exponential(
        18 * np.exp(0.4 * engagement) * (1 + 2.0 * full_14 + 0.8 * ai_14 + 0.3 * (call_day <= 13)))
    retained = rng.random(n) < _sigmoid(-2.2 + 0.6 * engagement + 1.5 * full_14 + 0.5 * ai_14)
    lifetime = np.where(retained, np.inf, lifetime)

    def adopt(p, daily_p):
        return np.where(rng.random(n) < p, _geometric_day(rng, daily_p, n), NEVER)

    return {
        'id': ids,
        'ws': ws_idx,
        'owner': owner,
        'signup': signup,
        'engagement': engagement,
        'is_admin': owner | (rng.random(n) < 0.10),
        'country': np.where(rng.random(n) < 0.8, ws['country'][ws_idx],
                            rng.choice(COUNTRIES, n, p=COUNTRY_PROBS)),
        'call_day': call_day,
        'ai_day': ai_day,
        'ai_voice': (ai_day < NEVER) & ws['voice_agent'][ws_idx] & (rng.random(n) < 0.5),
        'invite_day': invite_day,
        'invites_at_milestone': 1 + rng.poisson(0.8, n),
        'recording_day': np.where(called & (rng.random(n) < 0.5),
                                  call_day + _geometric_day(rng, 0.1, n), NEVER),
        'sms_day': adopt(0.65, 0.15),
        'whatsapp_day': adopt(0.35, 0.08),
        'integration_day': np.where(rng.random(n) < _sigmoid(-1.5 + 0.5 * engagement),
                                    _geometric_day(rng, 0.05, n), NEVER),
        'full_14': full_14,
        'lifetime': lifetime,
        'p_active': _sigmoid(0.2 + 0.6 * engagement),
        'gap_minutes': 1.5 + 2.5 * _sigmoid(engagement),
    }


# Recurring events per active day: (event, Poisson rate, gate attribute,
# extra condition). A user emits an event only on days >= the gate day, i.e.
# after adopting the feature.
RECURRING = [
    ('call.started', 1.8, 'call_day', None),
    ('call.recorded', 0.5, 'recording_day', None),
    ('sms.sent', 1.5, 'sms_day', None),
    ('sms.received', 1.2, 'sms_day', None),
    ('whatsapp.sent', 1.0, 'whatsapp_day', None),
    ('whatsapp.received', 0.8, 'whatsapp_day', None),
    ('ai_assist.used', 1.0, 'ai_day', 'assist'),
    ('ai_assist.summary_generated', 0.5, 'ai_day', 'assist'),
    ('ai_voice_agent.call_handled', 1.2, 'ai_day', 'voice'),
    ('integration.configured', 0.05, 'integration_day', None),
    ('team.member_invited', 0.01, 'invite_day', None),
]
ACCOUNT_EVENTS = ('user.signup', 'workspace.created', 'team.member_joined')


def _one_off_events(U, ws, lo, hi):
    """Milestone and account events as (local user idx, day since signup, event)."""
    sl = slice(lo, hi)
    owner = U['owner'][sl]
    voice = U['ai_voice'][sl]
    zeros = np.zeros(hi - lo, dtype=np.int64)
    ws_idx = U['ws'][sl]
    billing_day = ws['change_day'][ws_idx] - U['signup'][sl]
    upgraded = ws['upgraded'][ws_idx]
    specs = [
        ('user.signup', np.ones(hi - lo, bool), zeros, 1),
        ('workspace.created', owner, zeros, 1),
        ('team.member_joined', ~owner, zeros, 1),
        ('call.started', np.ones(hi - lo, bool), U['call_day'][sl], 1),
        ('ai_assist.used', ~voice, U['ai_day'][sl], 1),
        ('ai_voice_agent.activated', voice, U['ai_day'][sl], 1),
        ('team.member_invited', np.ones(hi - lo, bool), U['invite_day'][sl],
         U['invites_at_milestone'][sl]),
        ('integration.installed', np.ones(hi - lo, bool), U['integration_day'][sl], 1),
        ('billing.plan_upgraded', owner & upgraded, billing_day, 1),
        ('billing.plan_downgraded', owner & ~upgraded, billing_day, 1),
    ]
    out = []
    signup = U['signup'][sl]
    for name, mask, day, count in specs:
        keep = mask & (day >= 0) & (day < DAYS - signup)
        idx = np.flatnonzero(keep)
        reps = count[idx] if isinstance(count, np.ndarray) else count
        out.append((np.repeat(idx, reps), np.repeat(day[idx], reps), name))
    return out


def _generate_chunk(U, ws, lo, hi, rng, active_month):
    """Events and agent evaluations for users [lo, hi).

    Returns (events as a pyarrow Table, evaluations DataFrame, first product
    use per user in days since signup or NEVER).
    """
    n = hi - lo
    signup = U['signup'][lo:hi]
    t = np.arange(DAYS)

    # Which days each user is active, relative to signup: an early burst that
    # decays to a plateau, until the user churns (lifetime).
    p = U['p_active'][lo:hi, None] * (0.3 + 0.7 * np.exp(-t / 10.0))[None, :]
    active = (rng.random((n, DAYS)) < p) & (t[None, :] < U['lifetime'][lo:hi, None])
    active &= (signup[:, None] + t[None, :]) < DAYS
    one_offs = _one_off_events(U, ws, lo, hi)
    for idx, day, _ in one_offs:
        active[idx, day] = True  # milestones happen on active days

    ui, ti = np.nonzero(active)  # one row per active user-day
    gu = lo + ui
    row_of = np.full((n, DAYS), -1, dtype=np.int64)
    row_of[ui, ti] = np.arange(len(ui))

    rows, codes, firsts = [], [], []
    for idx, day, name in one_offs:
        rows.append(row_of[idx, day])
        codes.append(np.full(len(idx), EV[name], np.int16))
        firsts.append(np.full(len(idx), name in ACCOUNT_EVENTS))
    for name, rate, gate, kind in RECURRING:
        on = ti >= U[gate][gu]
        if kind == 'assist':
            on &= ~U['ai_voice'][gu]
        elif kind == 'voice':
            on &= U['ai_voice'][gu]
        r = np.repeat(np.arange(len(ui)), rng.poisson(np.where(on, rate, 0.0)))
        rows.append(r)
        codes.append(np.full(len(r), EV[name], np.int16))
        firsts.append(np.zeros(len(r), bool))
    r = np.repeat(np.arange(len(ui)), rng.poisson(ws['ticket_rate'][U['ws'][gu]]))
    rows.append(r)
    codes.append(np.full(len(r), EV['support.ticket_created'], np.int16))
    firsts.append(np.zeros(len(r), bool))

    row = np.concatenate(rows)
    code = np.concatenate(codes)
    is_first = np.concatenate(firsts)

    # Sessions: one or two per active day. Account events open the first
    # session; the rest are shuffled and spaced by exponential gaps.
    R = len(ui)
    n_sessions = 1 + (rng.random(R) < 0.3)
    session = np.where(is_first, 0, (rng.random(len(row)) * n_sessions[row]).astype(np.int64))
    order = np.lexsort((rng.random(len(row)), ~is_first, session, row))
    row, code, session = row[order], code[order], session[order]
    group = row * 2 + session
    starts = np.r_[True, group[1:] != group[:-1]]
    gaps = rng.exponential(U['gap_minutes'][gu[row]])
    gaps[starts] = 0
    cum = np.cumsum(gaps)
    offset = cum - cum[np.flatnonzero(starts)][np.cumsum(starts) - 1]
    day_start = rng.uniform(7 * 60, 18 * 60, R)
    second_session_delay = rng.uniform(150, 300, R)
    minutes = day_start[row] + session * second_session_delay[row] + offset
    seconds = np.minimum((minutes * 60).astype(np.int64) + rng.integers(0, 60, len(row)), 86399)

    # call.ended follows each call.started; most tickets get resolved.
    started = np.flatnonzero(code == EV['call.started'])
    tickets = np.flatnonzero(code == EV['support.ticket_created'])
    tickets = tickets[rng.random(len(tickets)) < 0.8]
    derived = np.concatenate([started, tickets])
    lag = np.concatenate([rng.exponential(240, len(started)),
                          rng.exponential(1200, len(tickets))]).astype(np.int64)
    row = np.concatenate([row, row[derived]])
    session = np.concatenate([session, session[derived]])
    seconds = np.concatenate([seconds, np.minimum(seconds[derived] + lag, 86399)])
    code = np.concatenate([code, np.full(len(started), EV['call.ended'], np.int16),
                           np.full(len(tickets), EV['support.ticket_resolved'], np.int16)])

    user = gu[row]
    day = signup[ui[row]] + ti[row]  # day of year (0-based)
    platform = rng.choice(len(PLATFORMS), 2 * R, p=PLATFORM_PROBS)[row * 2 + session]
    order = np.lexsort((seconds, day, user))
    session, seconds, code, user, day, platform = (
        a[order] for a in (session, seconds, code, user, day, platform))

    active_month[user, DAY_MONTH[day]] = True

    dates = START_DATE + day
    session_ids = (pd.Series(U['id'][user]) + '_' + pd.Series(day).astype(str).str.zfill(3)
                   + '_' + pd.Series(session).astype(str)).to_numpy()
    events = pa.table({
        'event_id': pa.array(_random_uuids(rng, len(user)), pa.string()),
        'user_id': pa.array(U['id'][user], pa.string()),
        'workspace_id': pa.array(ws['id'][U['ws'][user]], pa.string()),
        'event_name': pa.array(np.array(EVENT_TYPES, dtype=object)[code], pa.string()),
        'timestamp_utc': pa.array((dates.astype('datetime64[s]') + seconds)
                                  .astype('datetime64[us]')),
        'event_date': pa.array(dates, pa.date32()),
        'platform': pa.array(np.array(PLATFORMS, dtype=object)[platform], pa.string()),
        'country_code': pa.array(U['country'][user].astype(object), pa.string()),
        'session_id': pa.array(session_ids, pa.string()),
    })

    usage = ~np.isin(code, [EV[e] for e in ACCOUNT_EVENTS])
    first_use = np.full(n, NEVER, dtype=np.int64)
    np.minimum.at(first_use, user[usage] - lo, day[usage] - U['signup'][user[usage]])

    handled = np.flatnonzero(code == EV['ai_voice_agent.call_handled'])
    evals = _agent_evaluations(rng, ws, U['ws'][user[handled]], dates[handled])
    return events, evals, first_use


def _agent_evaluations(rng, ws, ws_idx, call_dates):
    """One evaluation per AI-handled call; quality depends on the workspace."""
    n = len(ws_idx)
    resolved = rng.random(n) < _sigmoid(-0.2 + 0.8 * ws['engagement'][ws_idx])
    escalated = ~resolved & (rng.random(n) < 0.6)
    handle = np.where(resolved, rng.lognormal(4.6, 0.6, n), rng.lognormal(5.3, 0.7, n))
    csat_mean = np.where(resolved, 4.3, np.where(escalated, 3.4, 3.8))
    return pd.DataFrame({
        'eval_id': _random_uuids(rng, n),
        'workspace_id': ws['id'][ws_idx],
        'call_date': call_dates.astype('datetime64[ns]'),
        'call_type': rng.choice(CALL_TYPES, n, p=CALL_TYPE_PROBS),
        'resolved_by_ai': resolved,
        'handle_time_seconds': handle.astype(int),
        'csat_score': np.clip(rng.normal(csat_mean, 0.6), 1, 5).round(1),
        'escalated_to_human': escalated,
    })


def generate_nps(U, ws, rng, seed):
    """NPS survey responses (0-10) at fixed days after signup."""
    frames = []
    for survey_day in NPS_SURVEY_DAYS:
        reached = (U['lifetime'] > survey_day) & (U['signup'] + survey_day < DAYS)
        respond = np.flatnonzero(reached & (rng.random(len(reached)) < NPS_RESPONSE_RATE))
        latent = (7.0 + 1.3 * U['engagement'][respond] + 0.8 * U['full_14'][respond]
                  - 0.5 * ws['ticket_rate'][U['ws'][respond]] / 0.03
                  + rng.normal(0, 1.5, len(respond)))
        day = np.minimum(U['signup'][respond] + survey_day + rng.integers(0, 7, len(respond)),
                         DAYS - 1)
        frames.append(pd.DataFrame({
            'user': respond, 'survey_day': survey_day,
            'response_date': (START_DATE + day).astype('datetime64[ns]'),
            'score': np.clip(np.round(latent), 0, 10).astype(int),
        }))
    nps = pd.concat(frames).sort_values(['user', 'survey_day'], kind='stable')
    return pd.DataFrame({
        'response_id': _stable_ids(seed, 'nps', len(nps)),
        'user_id': U['id'][nps['user'].to_numpy()],
        'workspace_id': ws['id'][U['ws'][nps['user'].to_numpy()]],
        'response_date': nps['response_date'].to_numpy(),
        'score': nps['score'].to_numpy(),
    })


def generate_subscriptions(U, ws, active_month):
    """Monthly per-active-seat billing for each workspace since its creation."""
    n_ws = len(ws['id'])
    billed = np.zeros((n_ws, N_MONTHS), dtype=np.int64)
    np.add.at(billed, U['ws'], active_month.astype(np.int64))
    w, m = np.meshgrid(np.arange(n_ws), np.arange(N_MONTHS), indexing='ij')
    w, m = w.ravel(), m.ravel()
    live = m >= DAY_MONTH[ws['created_day'][w]]
    w, m = w[live], m[live]
    change_month = np.where(ws['change_day'] < DAYS,
                            DAY_MONTH[np.minimum(ws['change_day'], DAYS - 1)], NEVER)
    plan = np.where(m >= change_month[w], ws['plan_final'][w], ws['plan_initial'][w])
    seats = billed[w, m]
    price = SEAT_PRICE_USD[plan]
    return pd.DataFrame({
        'workspace_id': ws['id'][w],
        'month_start': (START_DATE.astype('datetime64[M]') + m).astype('datetime64[ns]'),
        'plan_tier': np.array(PLAN_TIERS, dtype=object)[plan],
        'billed_seats': seats,
        'seat_price_usd': price,
        'mrr_usd': seats * price,
    })


def generate_dataset(out_dir='data', users=NUM_USERS, workspaces=None, seed=42,
                     max_evaluations=None, chunk_users=CHUNK_USERS, verbose=True):
    """Generate all files into out_dir. Returns row counts per file."""
    log = print if verbose else (lambda *a, **k: None)
    workspaces = workspaces or max(1, users // 10)
    os.makedirs(out_dir, exist_ok=True)
    rng = np.random.default_rng(seed)

    ws = generate_workspaces(workspaces, rng, seed)
    U = generate_users(users, ws, rng, seed)
    log(f'{workspaces:,} workspaces, {users:,} users')

    active_month = np.zeros((users, N_MONTHS), dtype=bool)
    first_use = np.empty(users, dtype=np.int64)
    eval_frames = []
    n_events = 0
    events_path = os.path.join(out_dir, 'events.parquet')
    writer = None
    try:
        for chunk, lo in enumerate(range(0, users, chunk_users)):
            hi = min(lo + chunk_users, users)
            chunk_rng = np.random.default_rng([seed, chunk])
            events, evals, first_use[lo:hi] = _generate_chunk(U, ws, lo, hi, chunk_rng,
                                                              active_month)
            if writer is None:
                writer = pq.ParquetWriter(events_path, events.schema)
            writer.write_table(events)
            eval_frames.append(evals)
            n_events += events.num_rows
            log(f'  users {lo:,}-{hi:,}: {events.num_rows:,} events (total {n_events:,})')
    finally:
        if writer is not None:
            writer.close()

    seats = np.maximum(ws['seats'], np.bincount(U['ws'], minlength=workspaces))
    pd.DataFrame({
        'workspace_id': ws['id'],
        'workspace_name': [f'Workspace_{i}' for i in range(workspaces)],
        'created_date': (START_DATE + ws['created_day']).astype('datetime64[ns]'),
        'plan_tier': np.array(PLAN_TIERS, dtype=object)[ws['plan_final']],
        'seat_count': seats,
        'country_code': ws['country'],
    }).to_parquet(os.path.join(out_dir, 'workspaces.parquet'), index=False)

    # first_active_date: first product use (account events excluded); NULL if never.
    first_active = (START_DATE + U['signup'] + np.minimum(first_use, DAYS)).astype('datetime64[ns]')
    first_active[first_use >= NEVER] = np.datetime64('NaT')
    pd.DataFrame({
        'user_id': U['id'],
        'workspace_id': ws['id'][U['ws']],
        'signup_date': (START_DATE + U['signup']).astype('datetime64[ns]'),
        'first_active_date': first_active,
        'plan_tier': np.array(PLAN_TIERS, dtype=object)[ws['plan_final'][U['ws']]],
        'country_code': U['country'],
        'is_admin': U['is_admin'],
    }).to_parquet(os.path.join(out_dir, 'users.parquet'), index=False)

    evals = pd.concat(eval_frames, ignore_index=True)
    if max_evaluations is not None and len(evals) > max_evaluations:
        keep = np.sort(rng.choice(len(evals), max_evaluations, replace=False))
        evals = evals.iloc[keep].reset_index(drop=True)
    evals.to_parquet(os.path.join(out_dir, 'agent_evaluations.parquet'), index=False)

    subs = generate_subscriptions(U, ws, active_month)
    subs.to_parquet(os.path.join(out_dir, 'subscriptions.parquet'), index=False)
    nps = generate_nps(U, ws, rng, seed)
    nps.to_parquet(os.path.join(out_dir, 'nps_responses.parquet'), index=False)

    counts = {'workspaces': workspaces, 'users': users, 'events': n_events,
              'agent_evaluations': len(evals), 'subscriptions': len(subs),
              'nps_responses': len(nps)}
    log('Wrote ' + ', '.join(f'{v:,} {k}' for k, v in counts.items()) + f' to {out_dir}/')
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate ConnectHub synthetic data')
    parser.add_argument('--users', type=int, default=NUM_USERS,
                        help=f'Number of users (default {NUM_USERS:,})')
    parser.add_argument('--workspaces', type=int, default=None,
                        help='Number of workspaces (default users / 10)')
    parser.add_argument('--evaluations', type=int, default=None,
                        help='Cap on agent evaluation records (default: one per '
                             'AI-handled call)')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--out-dir', default='data')
    args = parser.parse_args()
    generate_dataset(args.out_dir, args.users, args.workspaces, args.seed, args.evaluations)
