"""
Activation Funnel Analysis
Tracks new users through: Signup → First Call → AI Feature → Team Invited
"""
import pandas as pd
import plotly.graph_objects as go


def build_activation_funnel(conn, cohort_start, cohort_end):
    """Build 14-day activation funnel from the dbt intermediate model."""
    query = """
    SELECT
        COUNT(*) AS signed_up,
        SUM(placed_first_call) AS placed_first_call,
        SUM(used_ai_feature) AS used_ai_feature,
        SUM(invited_team_member) AS invited_team_member
    FROM intermediate.int_activation_funnel
    WHERE signup_date BETWEEN %(start)s AND %(end)s
    """
    result = pd.read_sql(query, conn, params={'start': cohort_start, 'end': cohort_end})

    stages = ['Signed Up', 'Placed First Call', 'Used AI Feature', 'Invited Team']
    values = result.iloc[0].tolist()

    fig = go.Figure(go.Funnel(
        y=stages, x=values,
        textinfo='value+percent initial',
        marker=dict(color=['#2563eb', '#3b82f6', '#60a5fa', '#93c5fd'])
    ))
    fig.update_layout(
        title='14-Day Activation Funnel (New Users)',
        font=dict(size=14)
    )
    return fig


def build_funnel_from_parquet(users_path, events_path):
    """Build activation funnel from local parquet files."""
    users = pd.read_parquet(users_path)
    events = pd.read_parquet(events_path)

    events['event_date'] = pd.to_datetime(events['event_date'])
    users['signup_date'] = pd.to_datetime(users['signup_date'])

    merged = events.merge(users[['user_id', 'signup_date']], on='user_id')
    merged['days_since_signup'] = (merged['event_date'] - merged['signup_date']).dt.days
    within_14d = merged[merged['days_since_signup'].between(0, 14)]

    total_signups = users['user_id'].nunique()
    placed_call = within_14d[within_14d['event_name'] == 'call.started']['user_id'].nunique()
    used_ai = within_14d[within_14d['event_name'].isin(
        ['ai_assist.used', 'ai_voice_agent.activated']
    )]['user_id'].nunique()
    invited_team = within_14d[within_14d['event_name'] == 'team.member_invited']['user_id'].nunique()

    stages = ['Signed Up', 'Placed First Call', 'Used AI Feature', 'Invited Team']
    values = [total_signups, placed_call, used_ai, invited_team]

    fig = go.Figure(go.Funnel(
        y=stages, x=values,
        textinfo='value+percent initial',
        marker=dict(color=['#2563eb', '#3b82f6', '#60a5fa', '#93c5fd'])
    ))
    fig.update_layout(title='14-Day Activation Funnel (New Users)', font=dict(size=14))
    return fig, dict(zip(stages, values, strict=True))
