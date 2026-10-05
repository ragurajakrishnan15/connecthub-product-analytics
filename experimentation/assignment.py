"""
Experiment Assignment Engine

Deterministic, reproducible assignment driven by the experiment registry
(experimentation/experiments.py):

- Two independent salted SHA-256 hashes per user: a *traffic* hash decides
  whether the user enters the experiment (traffic_pct), a *variant* hash
  (including the experiment's salt) picks the arm. Traffic and arm are
  therefore uncorrelated, and changing the salt reshuffles arms without
  changing who is in the experiment.
- Eligibility and assigned_at come from the data (signup date, or first
  activity on/after the start date), never from the clock, so reruns give
  identical assignments.
- Assignments are persisted to experiments.experiment_assignments with
  PRIMARY KEY (experiment_id, user_id) and INSERT ... ON CONFLICT DO NOTHING:
  rerunning never duplicates or reassigns a user.

CLI:
    python -m experimentation.assignment [--experiment-id ID ...] [--data-dir data]
                                         [--persist] [--no-parquet]
"""
import argparse
import hashlib
import io
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

from experimentation.experiments import EXPERIMENTS, get_experiment

TABLE = 'experiments.experiment_assignments'
COLUMNS = ['experiment_id', 'user_id', 'variant', 'assigned_at']
BUCKETS = 10_000


def _hash_int(*parts):
    return int.from_bytes(hashlib.sha256(':'.join(parts).encode()).digest()[:8], 'big')


def traffic_bucket(user_id, experiment_id):
    """0..9999; the user enters the experiment when bucket < traffic_pct * 10000."""
    return _hash_int(experiment_id, 'traffic', user_id) % BUCKETS


def assign_variant(user_id: str, experiment_id: str, num_variants: int = 2,
                   traffic_pct: float = 1.0, salt: str = 'v1') -> str:
    """Arm for one user: 'variant_<k>', or 'holdout' when outside the traffic."""
    if traffic_bucket(user_id, experiment_id) >= traffic_pct * BUCKETS:
        return 'holdout'
    return f'variant_{_hash_int(experiment_id, "variant", salt, user_id) % num_variants}'


def variants_for(experiment, user_ids):
    """Vectorized assign_variant for a registry Experiment."""
    return np.array([assign_variant(u, experiment.experiment_id, experiment.num_variants,
                                    experiment.traffic_pct, experiment.salt)
                     for u in user_ids], dtype=object)


def _first_activity(events_path, start, end):
    """First event timestamp per user within [start, end], streamed in batches."""
    dataset = ds.dataset(events_path)
    ts = ds.field('timestamp_utc')
    scanner = dataset.scanner(columns=['user_id', 'timestamp_utc'],
                              filter=(ts >= start.to_datetime64()) & (ts <= end.to_datetime64()))
    first = None
    for batch in scanner.to_batches():
        if batch.num_rows == 0:
            continue
        part = batch.to_pandas().groupby('user_id')['timestamp_utc'].min()
        first = part if first is None else pd.concat([first, part]).groupby(level=0).min()
    return first if first is not None else pd.Series(dtype='datetime64[us]')


def assign_users(experiment, users, events_path=None):
    """Assignments for one experiment: DataFrame[experiment_id, user_id, variant, assigned_at].

    users needs user_id and signup_date; 'active_users' experiments also need
    events_path. Users outside the traffic allocation are not returned.
    """
    if experiment.eligibility == 'new_signups':
        signup = pd.to_datetime(users['signup_date'])
        eligible = users[(signup >= experiment.start_ts) & (signup <= experiment.end_ts)]
        entered = pd.Series(pd.to_datetime(eligible['signup_date']).to_numpy(),
                            index=eligible['user_id'].to_numpy())
    elif experiment.eligibility == 'active_users':
        if events_path is None:
            raise ValueError(f'{experiment.experiment_id} needs events to find eligible users')
        entered = _first_activity(events_path, experiment.start_ts, experiment.end_ts)
        entered = entered[entered.index.isin(users['user_id'])]
    else:
        raise ValueError(f'Unknown eligibility {experiment.eligibility!r}')

    out = pd.DataFrame({
        'experiment_id': experiment.experiment_id,
        'user_id': entered.index.to_numpy(dtype=object),
        'variant': variants_for(experiment, entered.index),
        'assigned_at': pd.to_datetime(entered.to_numpy()).tz_localize('UTC'),
    })
    out = out[out['variant'] != 'holdout']
    return out.sort_values('user_id').reset_index(drop=True)


def assign_from_parquet(users_path, experiment_id, events_path=None, verbose=True):
    """Assignments for one experiment from local parquet files.

    events_path defaults to events.parquet next to users_path.
    """
    experiment = get_experiment(experiment_id)
    users = pd.read_parquet(users_path, columns=['user_id', 'signup_date'])
    events_path = events_path or os.path.join(os.path.dirname(str(users_path)), 'events.parquet')
    result = assign_users(experiment, users, events_path)
    if verbose:
        print(balance_summary(result))
    return result


def balance_summary(assignments):
    """One line per experiment: arm sizes and the SRM chi-square p-value."""
    from experimentation.stat_tests import srm_check
    lines = []
    for exp_id, grp in assignments.groupby('experiment_id'):
        counts = grp['variant'].value_counts().sort_index()
        line = f'{exp_id}: ' + ', '.join(f'{k}={v:,}' for k, v in counts.items())
        if len(counts) == 2:
            srm = srm_check(int(counts.iloc[0]), int(counts.iloc[1]))
            line += f" (SRM p={srm['p_value']:.4f})"
        lines.append(line)
    return '\n'.join(lines)


def ensure_table(engine, table=TABLE):
    """Create the assignments table with its primary key.

    An older table without the key (pre-Phase 3, loaded by ingestion) is
    replaced; assignments are deterministic, so they are simply recomputed.
    """
    from sqlalchemy import text
    schema, name = table.split('.')
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema}'))
        has_pk = conn.execute(text(
            'SELECT 1 FROM information_schema.table_constraints '
            "WHERE table_schema = :s AND table_name = :t AND constraint_type = 'PRIMARY KEY'"),
            {'s': schema, 't': name}).first()
        exists = conn.execute(text('SELECT to_regclass(:r)'), {'r': table}).scalar()
        if exists and not has_pk:
            conn.execute(text(f'DROP TABLE {table} CASCADE'))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                experiment_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                assigned_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (experiment_id, user_id)
            )"""))


def persist_assignments(engine, assignments, table=TABLE):
    """Insert new assignments; existing (experiment_id, user_id) rows are kept as-is.

    Returns the number of rows actually inserted.
    """
    ensure_table(engine, table)
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(f'CREATE TEMP TABLE new_assignments (LIKE {table}) ON COMMIT DROP')
            buf = io.StringIO()
            assignments[COLUMNS].to_csv(buf, index=False, header=False)
            buf.seek(0)
            cur.copy_expert(f'COPY new_assignments ({", ".join(COLUMNS)}) '
                            'FROM STDIN WITH (FORMAT csv)', buf)
            cur.execute(f'INSERT INTO {table} SELECT * FROM new_assignments '
                        'ON CONFLICT (experiment_id, user_id) DO NOTHING')
            inserted = cur.rowcount
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return inserted


def run(experiment_ids, data_dir='data', persist=False, write_parquet=True, engine=None):
    """Assign every requested experiment; optionally persist and write parquet."""
    users_path = os.path.join(data_dir, 'users.parquet')
    events_path = os.path.join(data_dir, 'events.parquet')
    users = pd.read_parquet(users_path, columns=['user_id', 'signup_date'])
    frames = [assign_users(get_experiment(e), users, events_path) for e in experiment_ids]
    assignments = pd.concat(frames, ignore_index=True)
    if write_parquet:
        assignments.to_parquet(os.path.join(data_dir, 'experiment_assignments.parquet'),
                               index=False)
    inserted = None
    if persist:
        if engine is None:
            from pipeline.config import create_engine
            engine = create_engine()
        inserted = persist_assignments(engine, assignments)
    return assignments, inserted


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m experimentation.assignment',
        description='Assign users to experiments from the registry.')
    parser.add_argument('--experiment-id', action='append', dest='experiments',
                        help='Experiment to assign (repeatable; default: all registered)')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--persist', action='store_true',
                        help='Insert into experiments.experiment_assignments (idempotent)')
    parser.add_argument('--no-parquet', action='store_true',
                        help='Do not write data/experiment_assignments.parquet')
    args = parser.parse_args(argv)

    experiments = args.experiments or sorted(EXPERIMENTS)
    try:
        assignments, inserted = run(experiments, args.data_dir, args.persist,
                                    not args.no_parquet)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(balance_summary(assignments))
    if inserted is not None:
        print(f'Persisted to {TABLE}: {inserted:,} new rows '
              f'({len(assignments) - inserted:,} already present)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
