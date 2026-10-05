"""
Experiment Assignment Engine
Deterministic hashing for consistent variant assignment.
"""
import hashlib
import pandas as pd


def assign_variant(
    user_id: str,
    experiment_id: str,
    num_variants: int = 2,
    traffic_pct: float = 1.0
) -> str:
    """Deterministic hashing for consistent variant assignment."""
    hash_input = f"{experiment_id}:{user_id}"
    hash_val = int(hashlib.sha256(hash_input.encode()).hexdigest(), 16)
    bucket = hash_val % 10000

    if bucket >= traffic_pct * 10000:
        return 'holdout'

    variant_bucket = hash_val % num_variants
    return f'variant_{variant_bucket}'


def assign_experiment_cohort(conn, experiment_id, start_date, end_date):
    """Assign users active in the date range who are not yet in the experiment.

    Returns the new assignments; persist them to data/experiment_assignments.parquet
    and load with `python scripts/ingest_events.py --load-postgres`
    (table experiments.experiment_assignments).
    """
    query = """
    SELECT DISTINCT user_id
    FROM staging.stg_events
    WHERE event_date BETWEEN %(start)s AND %(end)s
      AND user_id NOT IN (
          SELECT user_id FROM gold.fct_experiment_assignments
          WHERE experiment_id = %(exp_id)s
      )
    """
    users = pd.read_sql(query, conn, params={
        'start': start_date, 'end': end_date, 'exp_id': experiment_id
    })

    users['variant'] = users['user_id'].apply(
        lambda uid: assign_variant(uid, experiment_id)
    )
    users['experiment_id'] = experiment_id
    users['assigned_at'] = pd.Timestamp.now(tz='UTC')
    return users


def assign_from_parquet(users_path, experiment_id, num_variants=2):
    """Assign variants from local parquet for testing."""
    users = pd.read_parquet(users_path)
    users['variant'] = users['user_id'].apply(
        lambda uid: assign_variant(uid, experiment_id, num_variants)
    )
    users['experiment_id'] = experiment_id
    users['assigned_at'] = pd.Timestamp.now(tz='UTC')

    # Verify balance
    dist = users['variant'].value_counts()
    print(f"Experiment: {experiment_id}")
    print(f"Variant distribution:\n{dist}")
    print(f"Ratio: {dist.min() / dist.max():.4f} (should be close to 1.0)")
    return users


if __name__ == '__main__':
    result = assign_from_parquet('data/users.parquet', 'exp_onboarding_v2')
    result.to_parquet('data/experiment_assignments.parquet', index=False)
