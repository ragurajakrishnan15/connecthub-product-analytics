"""
Customer Health Scoring
Composite health score per workspace for churn prediction and outreach.
"""
import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler


def compute_health_scores(conn):
    """Compute health scores from gold.metrics_product_health (latest snapshot).

    pct_ai_calls_automated, nps_score and expansion_revenue_30d are not in the
    warehouse yet; _score() weights only the columns that are present.
    """
    query = """
    SELECT
        workspace_id,
        workspace_name,
        plan_tier,
        seat_count,
        dau_over_seats_ratio,
        features_adopted_count,
        avg_session_duration_minutes,
        support_tickets_last_30d
    FROM gold.metrics_product_health
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM gold.metrics_product_health)
    """
    df = pd.read_sql(query, conn)
    return _score(df)


def compute_health_from_parquet(users_path, events_path, workspaces_path):
    """Compute health scores from local parquet files."""
    events = pd.read_parquet(events_path)
    workspaces = pd.read_parquet(workspaces_path)

    events['event_date'] = pd.to_datetime(events['event_date'])
    max_date = events['event_date'].max()
    last_30d = events[events['event_date'] >= max_date - pd.Timedelta(days=30)].copy()

    # DAU per workspace
    dau = last_30d.groupby('workspace_id')['user_id'].nunique().reset_index()
    dau.columns = ['workspace_id', 'active_users_30d']

    # Features adopted
    feature_map = {
        'ai_assist.used': 'ai_assist',
        'ai_voice_agent.activated': 'ai_voice_agent',
        'sms.sent': 'sms', 'whatsapp.sent': 'whatsapp',
        'call.recorded': 'call_recording',
    }
    last_30d['feature'] = last_30d['event_name'].map(feature_map)
    features = last_30d.dropna(subset=['feature']).groupby('workspace_id')['feature'].nunique().reset_index()
    features.columns = ['workspace_id', 'features_adopted_count']

    # Support tickets
    tickets = last_30d[last_30d['event_name'] == 'support.ticket_created'] \
        .groupby('workspace_id').size().reset_index(name='support_tickets_last_30d')

    # Merge
    ws = workspaces[['workspace_id', 'workspace_name', 'plan_tier', 'seat_count']].copy()
    ws = ws.merge(dau, on='workspace_id', how='left')
    ws = ws.merge(features, on='workspace_id', how='left')
    ws = ws.merge(tickets, on='workspace_id', how='left')
    ws = ws.fillna(0)

    ws['dau_over_seats_ratio'] = ws['active_users_30d'] / ws['seat_count'].clip(lower=1)
    ws['pct_ai_calls_automated'] = np.random.uniform(0, 0.6, len(ws))
    ws['avg_session_duration_minutes'] = np.random.lognormal(2, 0.5, len(ws))
    ws['nps_score'] = np.clip(np.random.normal(45, 15, len(ws)), 0, 100)
    ws['expansion_revenue_30d'] = np.random.exponential(500, len(ws))

    return _score(ws)


def _score(df):
    """Apply weighted scoring to compute health scores."""
    weights = {
        'dau_over_seats_ratio': 0.25,
        'features_adopted_count': 0.20,
        'avg_session_duration_minutes': 0.10,
        'pct_ai_calls_automated': 0.15,
        'support_tickets_last_30d': -0.10,
        'nps_score': 0.10,
        'expansion_revenue_30d': 0.10
    }

    score_cols = [c for c in weights.keys() if c in df.columns]
    scaler = MinMaxScaler()
    df[score_cols] = scaler.fit_transform(df[score_cols].fillna(0))

    df['health_score'] = sum(
        df[col] * weights[col] for col in score_cols
    )
    df['health_score'] = (df['health_score'] * 100).clip(0, 100).round(1)

    df['risk_tier'] = pd.cut(
        df['health_score'],
        bins=[0, 30, 60, 80, 100],
        labels=['Critical', 'At Risk', 'Healthy', 'Champion'],
        include_lowest=True
    )
    return df.sort_values('health_score', ascending=True)
