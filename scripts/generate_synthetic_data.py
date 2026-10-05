"""
ConnectHub Synthetic Data Generator
Generates 500K users and 50M+ product events for the analytics platform.
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import uuid
import os
import argparse

np.random.seed(42)

NUM_USERS = 500_000
NUM_WORKSPACES = 50_000
DAYS = 365
START_DATE = datetime(2025, 1, 1)

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

PLAN_TIERS = ['Free', 'Essentials', 'Professional', 'Enterprise']
PLATFORMS = ['web', 'mobile', 'api']

CALL_TYPES = ['inbound', 'outbound', 'internal', 'voicemail']
COUNTRIES = ['US', 'FR', 'DE', 'GB', 'ES', 'AU', 'MX', 'BR']
COUNTRY_PROBS = [0.30, 0.15, 0.12, 0.10, 0.08, 0.08, 0.10, 0.07]


def generate_workspaces(n):
    """Generate workspace dimension table."""
    workspaces = pd.DataFrame({
        'workspace_id': [str(uuid.uuid4()) for _ in range(n)],
        'workspace_name': [f'Workspace_{i}' for i in range(n)],
        'created_date': [
            START_DATE + timedelta(days=int(np.random.exponential(60)))
            for _ in range(n)
        ],
        'plan_tier': np.random.choice(
            PLAN_TIERS, n, p=[0.30, 0.35, 0.25, 0.10]
        ),
        'seat_count': np.random.choice(
            [1, 3, 5, 10, 15, 25, 50, 100], n,
            p=[0.10, 0.15, 0.20, 0.20, 0.15, 0.10, 0.07, 0.03]
        ),
        'country_code': np.random.choice(COUNTRIES, n, p=COUNTRY_PROBS)
    })
    return workspaces


def generate_users(n, workspaces):
    """Generate user dimension table."""
    workspace_ids = workspaces['workspace_id'].values
    users = pd.DataFrame({
        'user_id': [str(uuid.uuid4()) for _ in range(n)],
        'workspace_id': np.random.choice(workspace_ids, n),
        'signup_date': [
            START_DATE + timedelta(days=int(np.random.exponential(90)))
            for _ in range(n)
        ],
        'plan_tier': np.random.choice(
            PLAN_TIERS, n, p=[0.30, 0.35, 0.25, 0.10]
        ),
        'country_code': np.random.choice(COUNTRIES, n, p=COUNTRY_PROBS),
        'is_admin': np.random.choice([True, False], n, p=[0.15, 0.85])
    })
    # Clip signup dates to valid range
    end_date = START_DATE + timedelta(days=DAYS)
    users['signup_date'] = users['signup_date'].clip(
        upper=end_date
    )
    users['first_active_date'] = users['signup_date'] + pd.to_timedelta(
        np.random.randint(0, 3, n), unit='D'
    )
    return users


def generate_events(users, days=365, batch_size=100_000):
    """Generate product events in batches to manage memory."""
    all_events = []
    user_array = users[['user_id', 'workspace_id', 'signup_date', 'country_code']].values

    print(f"Generating events for {len(users):,} users...")

    for batch_start in range(0, len(user_array), batch_size):
        batch = user_array[batch_start:batch_start + batch_size]
        batch_events = []

        for user_id, workspace_id, signup_date, country_code in batch:
            user_start = max(signup_date, START_DATE)
            # Power users generate more events
            n_events = int(np.random.exponential(100))
            n_events = min(n_events, 500)  # cap per user

            for _ in range(n_events):
                event_offset_days = int(np.random.uniform(0, days))
                event_date = user_start + timedelta(
                    days=event_offset_days,
                    hours=int(np.random.uniform(8, 22)),
                    minutes=int(np.random.uniform(0, 60)),
                    seconds=int(np.random.uniform(0, 60))
                )

                # Clip to valid range
                end_date = START_DATE + timedelta(days=days)
                if event_date > end_date:
                    continue

                batch_events.append({
                    'event_id': str(uuid.uuid4()),
                    'user_id': user_id,
                    'workspace_id': workspace_id,
                    'event_name': np.random.choice(EVENT_TYPES),
                    'timestamp_utc': event_date,
                    'event_date': event_date.date(),
                    'platform': np.random.choice(PLATFORMS, p=[0.55, 0.35, 0.10]),
                    'country_code': country_code,
                    'session_id': f"{user_id}_{event_offset_days // 1}"
                })

        all_events.extend(batch_events)
        print(f"  Batch {batch_start // batch_size + 1}: "
              f"{len(batch_events):,} events generated "
              f"(total: {len(all_events):,})")

    return pd.DataFrame(all_events)


def generate_agent_evaluations(n=500_000):
    """Generate AI agent evaluation records."""
    evals = pd.DataFrame({
        'eval_id': [str(uuid.uuid4()) for _ in range(n)],
        'call_date': [
            START_DATE + timedelta(days=int(np.random.uniform(0, DAYS)))
            for _ in range(n)
        ],
        'call_type': np.random.choice(
            CALL_TYPES, n, p=[0.40, 0.30, 0.15, 0.15]
        ),
        'resolved_by_ai': np.random.choice(
            [True, False], n, p=[0.35, 0.65]
        ),
        'handle_time_seconds': np.random.lognormal(mean=5.0, sigma=0.8, size=n).astype(int),
        'csat_score': np.clip(np.random.normal(4.0, 0.8, n), 1, 5).round(1),
        'escalated_to_human': np.random.choice(
            [True, False], n, p=[0.25, 0.75]
        )
    })
    return evals


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate ConnectHub synthetic data')
    parser.add_argument('--users', type=int, default=NUM_USERS,
                        help=f'Number of users (default {NUM_USERS:,}; the default '
                             'needs tens of GB of RAM, use e.g. 2000 for local dev)')
    parser.add_argument('--workspaces', type=int, default=NUM_WORKSPACES)
    parser.add_argument('--evaluations', type=int, default=500_000,
                        help='Number of AI agent evaluation records')
    args = parser.parse_args()
    NUM_USERS, NUM_WORKSPACES = args.users, args.workspaces

    os.makedirs('data', exist_ok=True)

    print("=" * 60)
    print("ConnectHub Synthetic Data Generator")
    print("=" * 60)

    print("\n1. Generating workspaces...")
    workspaces = generate_workspaces(NUM_WORKSPACES)
    workspaces.to_parquet('data/workspaces.parquet', index=False)
    print(f"   -> {len(workspaces):,} workspaces")

    print("\n2. Generating users...")
    users = generate_users(NUM_USERS, workspaces)
    users.to_parquet('data/users.parquet', index=False)
    print(f"   -> {len(users):,} users")

    print("\n3. Generating events (this takes a few minutes)...")
    events = generate_events(users, DAYS)
    events.to_parquet('data/events.parquet', index=False)
    print(f"   -> {len(events):,} events")

    print("\n4. Generating agent evaluations...")
    evals = generate_agent_evaluations(args.evaluations)
    evals.to_parquet('data/agent_evaluations.parquet', index=False)
    print(f"   -> {len(evals):,} evaluations")

    print("\n" + "=" * 60)
    print("DONE! Generated:")
    print(f"  {len(workspaces):,} workspaces")
    print(f"  {len(users):,} users")
    print(f"  {len(events):,} events")
    print(f"  {len(evals):,} agent evaluations")
    print("\nFiles saved to data/")
    print("=" * 60)
