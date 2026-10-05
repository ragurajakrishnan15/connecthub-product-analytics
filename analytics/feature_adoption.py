"""
Feature Adoption Tracking
Tracks adoption curves for major features across cohorts.
"""
import pandas as pd
import plotly.express as px


def feature_adoption_curves(conn, features=None):
    """Generate feature adoption curves from gold tables."""
    if features is None:
        features = ['ai_assist', 'ai_voice_agent', 'whatsapp', 'sms', 'call_recording']

    query = """
    SELECT
        feature_name,
        days_since_signup,
        cumulative_adoption_pct
    FROM gold.fct_feature_adoption
    WHERE feature_name = ANY(%(features)s)
      AND days_since_signup <= 90
    ORDER BY feature_name, days_since_signup
    """
    df = pd.read_sql(query, conn, params={'features': features})

    fig = px.line(
        df, x='days_since_signup', y='cumulative_adoption_pct',
        color='feature_name',
        title='Feature Adoption Curves (First 90 Days)',
        labels={
            'days_since_signup': 'Days Since Signup',
            'cumulative_adoption_pct': 'Cumulative Adoption (%)',
            'feature_name': 'Feature'
        }
    )
    fig.update_layout(legend=dict(orientation='h', yanchor='bottom', y=1.02))
    return fig


def adoption_from_parquet(users_path, events_path, max_days=90):
    """Generate feature adoption curves from local parquet files."""
    users = pd.read_parquet(users_path)
    events = pd.read_parquet(events_path)

    feature_map = {
        'ai_assist.used': 'ai_assist',
        'ai_assist.summary_generated': 'ai_assist',
        'ai_voice_agent.activated': 'ai_voice_agent',
        'ai_voice_agent.call_handled': 'ai_voice_agent',
        'sms.sent': 'sms', 'sms.received': 'sms',
        'whatsapp.sent': 'whatsapp', 'whatsapp.received': 'whatsapp',
        'call.recorded': 'call_recording',
    }

    events['feature_name'] = events['event_name'].map(feature_map)
    feature_events = events.dropna(subset=['feature_name'])

    merged = feature_events.merge(users[['user_id', 'signup_date']], on='user_id')
    merged['event_date'] = pd.to_datetime(merged['event_date'])
    merged['signup_date'] = pd.to_datetime(merged['signup_date'])
    merged['days_since_signup'] = (merged['event_date'] - merged['signup_date']).dt.days

    within_range = merged[merged['days_since_signup'].between(0, max_days)]

    # First adoption per user per feature
    first_adoption = within_range.groupby(['user_id', 'feature_name'])['days_since_signup'].min().reset_index()
    total_users = users['user_id'].nunique()

    adoption_curves = []
    for feature in first_adoption['feature_name'].unique():
        feat_data = first_adoption[first_adoption['feature_name'] == feature]
        for day in range(max_days + 1):
            adopted = (feat_data['days_since_signup'] <= day).sum()
            adoption_curves.append({
                'feature_name': feature,
                'days_since_signup': day,
                'cumulative_adoption_pct': round(adopted / total_users * 100, 2)
            })

    df = pd.DataFrame(adoption_curves)

    fig = px.line(
        df, x='days_since_signup', y='cumulative_adoption_pct',
        color='feature_name',
        title='Feature Adoption Curves (First 90 Days)',
        labels={
            'days_since_signup': 'Days Since Signup',
            'cumulative_adoption_pct': 'Cumulative Adoption (%)',
            'feature_name': 'Feature'
        }
    )
    fig.update_layout(legend=dict(orientation='h', yanchor='bottom', y=1.02))
    return fig, df
