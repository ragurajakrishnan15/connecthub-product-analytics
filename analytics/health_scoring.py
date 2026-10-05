"""
Customer Health Scoring
Composite health score per workspace for churn prediction and outreach.

Inputs are the columns of gold.metrics_product_health; the parquet path
recomputes the same inputs from local files. Each input is converted to a
percentile rank across workspaces (support tickets per active user: fewer is
better) and the score is the weighted average of the ranks, 0-100. Inputs a
workspace has no signal for (no calls, no NPS responses, no billing) are left
out of its average instead of counting as zero. Scores are relative to the
workspaces scored together.

CLI:
    python -m analytics.health_scoring [--as-of YYYY-MM-DD] [--source warehouse|parquet]
        [--data-dir data] [--persist] [--output scores.csv] [--json]
--persist replaces analytics.workspace_health_scores rows for the snapshot date
(idempotent). Exit codes: 0 ok, 1 no data or error, 2 usage error.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

from analytics.features import AI_FEATURES, FEATURE_MAP

WEIGHTS = {
    'dau_over_seats_ratio': 0.25,
    'features_adopted_count': 0.20,
    'pct_ai_calls_automated': 0.15,
    'avg_session_duration_minutes': 0.10,
    'nps_score': 0.10,
    'mrr_change_usd': 0.10,
    'tickets_per_active_user': 0.10,
}
LOWER_IS_BETTER = {'tickets_per_active_user'}

# Calibrated on the synthetic data by backtesting: scores as of 2025-11-30
# against which workspaces had no active users in December (see PHASE_2_REPORT.md).
TIER_BINS = [0, 40, 55, 70, 100]
TIER_LABELS = ['Critical', 'At Risk', 'Healthy', 'Champion']

INPUT_COLUMNS = [
    'workspace_id', 'workspace_name', 'plan_tier', 'seat_count', 'snapshot_date',
    'active_users_30d', 'dau_over_seats_ratio', 'features_adopted_count',
    'used_ai_feature_30d', 'avg_session_duration_minutes', 'support_tickets_last_30d',
    'pct_ai_calls_automated', 'nps_score', 'nps_responses_90d', 'mrr_usd', 'mrr_change_usd',
]


def compute_health_scores(conn):
    """Compute health scores from gold.metrics_product_health (latest snapshot)."""
    query = f"""
    SELECT {', '.join(INPUT_COLUMNS)}
    FROM gold.metrics_product_health
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM gold.metrics_product_health)
    """
    df = pd.read_sql(query, conn)
    return _score(df)


def health_inputs_from_parquet(data_dir='data', as_of=None):
    """Workspace health inputs from parquet; mirrors gold.metrics_product_health.

    as_of (date-like) computes the snapshot at an earlier date, ignoring later
    events, for backtesting. Revenue uses the as_of month's MRR.
    """
    def read(name, **kw):
        return pd.read_parquet(os.path.join(data_dir, name), **kw)

    events = read('events.parquet', columns=[
        'event_id', 'user_id', 'workspace_id', 'event_name', 'timestamp_utc',
        'event_date', 'session_id']).drop_duplicates('event_id')
    events['event_date'] = pd.to_datetime(events['event_date'])
    snapshot = pd.Timestamp(as_of) if as_of is not None else events['event_date'].max()
    events = events[events['event_date'] <= snapshot]
    start_30d = snapshot - pd.Timedelta(days=30)
    recent = events[events['event_date'] > start_30d]

    activity = recent.groupby('workspace_id').agg(
        active_users_30d=('user_id', 'nunique'),
        support_tickets_last_30d=('event_name', lambda s: (s == 'support.ticket_created').sum()))

    feats = recent.assign(feature=recent['event_name'].map(FEATURE_MAP)).dropna(subset=['feature'])
    features = feats.groupby('workspace_id').agg(
        features_adopted_count=('feature', 'nunique'),
        used_ai_feature_30d=('feature', lambda s: s.isin(AI_FEATURES).any()))

    sessions = events.groupby(['session_id', 'user_id', 'workspace_id']).agg(
        start=('timestamp_utc', 'min'), end=('timestamp_utc', 'max'),
        session_date=('event_date', 'min')).reset_index()
    sessions = sessions[sessions['session_date'] > start_30d]
    sessions['minutes'] = (sessions['end'] - sessions['start']).dt.total_seconds() / 60
    session_minutes = sessions.groupby('workspace_id')['minutes'].mean() \
        .rename('avg_session_duration_minutes')

    evals = read('agent_evaluations.parquet', columns=[
        'workspace_id', 'call_date', 'resolved_by_ai', 'escalated_to_human'])
    evals['call_date'] = pd.to_datetime(evals['call_date'])
    evals = evals[(evals['call_date'] > start_30d) & (evals['call_date'] <= snapshot)]
    ai_resolved = (evals['resolved_by_ai'] & ~evals['escalated_to_human']).groupby(
        evals['workspace_id']).sum()
    total_calls = evals.groupby('workspace_id').size().add(
        recent[recent['event_name'] == 'call.started'].groupby('workspace_id').size(),
        fill_value=0)
    pct_ai = (ai_resolved.reindex(total_calls.index, fill_value=0) / total_calls) \
        .rename('pct_ai_calls_automated')

    nps = read('nps_responses.parquet', columns=['workspace_id', 'response_date', 'score'])
    nps['response_date'] = pd.to_datetime(nps['response_date'])
    nps = nps[(nps['response_date'] > snapshot - pd.Timedelta(days=90))
              & (nps['response_date'] <= snapshot)]
    nps_by_ws = nps.groupby('workspace_id')['score'].agg(
        nps_responses_90d='size',
        nps_score=lambda s: round(100.0 * ((s >= 9).sum() - (s <= 6).sum()) / len(s), 1))

    subs = read('subscriptions.parquet', columns=['workspace_id', 'month_start', 'mrr_usd'])
    subs['month_start'] = pd.to_datetime(subs['month_start'])
    month = snapshot.to_period('M').start_time
    current = subs[subs['month_start'] == month].set_index('workspace_id')['mrr_usd']
    previous = subs[subs['month_start'] == month - pd.offsets.MonthBegin(1)] \
        .set_index('workspace_id')['mrr_usd']
    revenue = pd.DataFrame({'mrr_usd': current,
                            'mrr_change_usd': current - previous.reindex(current.index).fillna(0)})

    ws = read('workspaces.parquet',
              columns=['workspace_id', 'workspace_name', 'plan_tier', 'seat_count'])
    ws = ws.set_index('workspace_id').join([activity, features, session_minutes, pct_ai,
                                            nps_by_ws, revenue]).reset_index()
    ws['snapshot_date'] = snapshot.date()
    zero_fill = {'active_users_30d': 0, 'support_tickets_last_30d': 0,
                 'features_adopted_count': 0, 'avg_session_duration_minutes': 0.0,
                 'nps_responses_90d': 0, 'mrr_usd': 0.0, 'mrr_change_usd': 0.0}
    ws = ws.fillna(zero_fill)
    ws['used_ai_feature_30d'] = ws['used_ai_feature_30d'].astype('boolean').fillna(False)         .astype(bool)
    for col in ('active_users_30d', 'support_tickets_last_30d', 'features_adopted_count',
                'nps_responses_90d'):
        ws[col] = ws[col].astype(int)
    ws['dau_over_seats_ratio'] = (ws['active_users_30d'] / ws['seat_count'].clip(lower=1)).round(4)
    ws['avg_session_duration_minutes'] = ws['avg_session_duration_minutes'].round(2)
    ws['pct_ai_calls_automated'] = ws['pct_ai_calls_automated'].astype(float).round(4)
    return ws[INPUT_COLUMNS]


def compute_health_from_parquet(users_path, events_path, workspaces_path, as_of=None):
    """Compute health scores from local parquet files.

    The other inputs (agent_evaluations, nps_responses, subscriptions) are read
    from the same directory as events_path. users_path is kept for compatibility.
    """
    return _score(health_inputs_from_parquet(os.path.dirname(events_path) or '.', as_of))


def _score(df):
    """Weighted average of percentile ranks, 0-100, plus a risk tier."""
    df = df.copy()
    # NUMERIC columns arrive from PostgreSQL as Decimal; score on floats.
    signals = pd.DataFrame({c: df[c].astype(float) for c in WEIGHTS if c in df.columns},
                           index=df.index)
    if {'support_tickets_last_30d', 'active_users_30d'} <= set(df.columns):
        active = df['active_users_30d'].astype(float)
        signals['tickets_per_active_user'] = (df['support_tickets_last_30d'].astype(float)
                                              / active.where(active > 0))
    if {'mrr_usd', 'mrr_change_usd'} <= set(df.columns):
        # No billing in either month (e.g. Free plan): no revenue signal.
        no_revenue = (df['mrr_usd'].astype(float) == 0) & (df['mrr_change_usd'].astype(float) == 0)
        signals['mrr_change_usd'] = signals['mrr_change_usd'].mask(no_revenue)

    cols = [c for c in WEIGHTS if c in signals.columns]
    ranks = pd.DataFrame({
        c: signals[c].rank(pct=True, ascending=c not in LOWER_IS_BETTER) for c in cols
    })
    weights = pd.Series(WEIGHTS)[cols]
    weighted = (ranks.fillna(0) * weights).sum(axis=1)
    available = ranks.notna().mul(weights).sum(axis=1)
    df['health_score'] = (100 * weighted / available.replace(0, np.nan)).round(1)
    df['risk_tier'] = pd.cut(df['health_score'], bins=TIER_BINS, labels=TIER_LABELS,
                             include_lowest=True)
    return df.sort_values('health_score', ascending=True)


PERSIST_TABLE = 'analytics.workspace_health_scores'
PERSIST_COLUMNS = INPUT_COLUMNS + ['health_score', 'risk_tier']


def persist_scores(engine, scores):
    """Replace the snapshot's rows in analytics.workspace_health_scores (idempotent)."""
    from sqlalchemy import text
    out = scores[PERSIST_COLUMNS].copy()
    out['risk_tier'] = out['risk_tier'].astype(str)
    out['snapshot_date'] = pd.to_datetime(out['snapshot_date']).dt.date
    snapshots = sorted(out['snapshot_date'].unique())
    with engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS analytics'))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {PERSIST_TABLE} (
                workspace_id TEXT NOT NULL, workspace_name TEXT, plan_tier TEXT,
                seat_count INTEGER, snapshot_date DATE NOT NULL,
                active_users_30d INTEGER, dau_over_seats_ratio DOUBLE PRECISION,
                features_adopted_count INTEGER, used_ai_feature_30d BOOLEAN,
                avg_session_duration_minutes DOUBLE PRECISION,
                support_tickets_last_30d INTEGER, pct_ai_calls_automated DOUBLE PRECISION,
                nps_score DOUBLE PRECISION, nps_responses_90d INTEGER,
                mrr_usd DOUBLE PRECISION, mrr_change_usd DOUBLE PRECISION,
                health_score DOUBLE PRECISION, risk_tier TEXT,
                PRIMARY KEY (snapshot_date, workspace_id)
            )"""))
        conn.execute(text(f'DELETE FROM {PERSIST_TABLE} WHERE snapshot_date = ANY(:d)'),
                     {'d': snapshots})
        out.to_sql(PERSIST_TABLE.split('.')[1], conn, schema='analytics', if_exists='append',
                   index=False, method='multi', chunksize=5000)
    return len(out)


def tier_counts(scores):
    return {str(k): int(v) for k, v in scores['risk_tier'].value_counts().sort_index().items()}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m analytics.health_scoring',
                                     description='Workspace health scores and risk tiers.')
    parser.add_argument('--as-of', help='Snapshot date (parquet source only; '
                                        'the warehouse holds the latest snapshot)')
    parser.add_argument('--source', choices=['warehouse', 'parquet'], default='warehouse')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--persist', action='store_true',
                        help=f'Replace this snapshot in {PERSIST_TABLE}')
    parser.add_argument('--output', help='Write scores to this CSV')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.as_of and args.source == 'warehouse':
        parser.error('--as-of needs --source parquet (gold.metrics_product_health '
                     'holds only the latest snapshot)')

    engine = None
    try:
        if args.source == 'parquet':
            scores = _score(health_inputs_from_parquet(args.data_dir, args.as_of))
        else:
            from pipeline.config import create_engine
            engine = create_engine()
            scores = compute_health_scores(engine)
    except Exception as exc:
        print(f'error: could not compute health scores from {args.source}: {exc}',
              file=sys.stderr)
        return 1
    if scores.empty:
        print('error: no workspaces to score', file=sys.stderr)
        return 1

    summary = {
        'snapshot_date': str(scores['snapshot_date'].iloc[0]),
        'workspaces': int(len(scores)),
        'tiers': tier_counts(scores),
        'mean_health_score': round(float(scores['health_score'].mean()), 1),
    }
    if args.persist:
        if engine is None:
            from pipeline.config import create_engine
            engine = create_engine()
        summary['persisted_rows'] = persist_scores(engine, scores)
    if args.output:
        scores.to_csv(args.output, index=False)
    if args.json:
        print(json.dumps(summary))
    else:
        tiers = ', '.join(f'{k} {v}' for k, v in summary['tiers'].items())
        print(f"Health scores for {summary['workspaces']:,} workspaces as of "
              f"{summary['snapshot_date']}: {tiers} (mean {summary['mean_health_score']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
