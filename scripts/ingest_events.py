"""
Event Ingestion Script
Pulls events from Kafka/Kinesis and writes to S3 raw zone as Parquet.
For local dev, reads from synthetic data and loads into Postgres.

Warehouse layout (local PostgreSQL):
    bronze.*        raw tables loaded by this script
    experiments.*   experiment assignments loaded by this script
    staging / intermediate / gold / semantic   built by dbt (dbt_project/)
"""
import argparse
import io
import os

import pandas as pd
import pyarrow.parquet as pq
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# Explicit DDL so column types do not depend on pandas type inference.
# Order of columns here is the order used for COPY.
TABLES = {
    'bronze.events_raw': {
        'file': 'events.parquet',
        'columns': {
            'event_id': 'TEXT NOT NULL',
            'user_id': 'TEXT NOT NULL',
            'workspace_id': 'TEXT',
            'event_name': 'TEXT NOT NULL',
            'timestamp_utc': 'TIMESTAMP NOT NULL',
            'event_date': 'DATE NOT NULL',
            'platform': 'TEXT',
            'country_code': 'TEXT',
            'session_id': 'TEXT',
        },
    },
    'bronze.users_raw': {
        'file': 'users.parquet',
        'columns': {
            'user_id': 'TEXT PRIMARY KEY',
            'workspace_id': 'TEXT',
            'signup_date': 'DATE NOT NULL',
            'first_active_date': 'DATE',
            'plan_tier': 'TEXT',
            'country_code': 'TEXT',
            'is_admin': 'BOOLEAN',
        },
    },
    'bronze.workspaces_raw': {
        'file': 'workspaces.parquet',
        'columns': {
            'workspace_id': 'TEXT PRIMARY KEY',
            'workspace_name': 'TEXT',
            'created_date': 'DATE',
            'plan_tier': 'TEXT',
            'seat_count': 'INTEGER',
            'country_code': 'TEXT',
        },
    },
    'bronze.agent_evaluations': {
        'file': 'agent_evaluations.parquet',
        'columns': {
            'eval_id': 'TEXT PRIMARY KEY',
            'workspace_id': 'TEXT',
            'call_date': 'DATE NOT NULL',
            'call_type': 'TEXT',
            'resolved_by_ai': 'BOOLEAN',
            'handle_time_seconds': 'INTEGER',
            'csat_score': 'NUMERIC(3, 1)',
            'escalated_to_human': 'BOOLEAN',
        },
    },
    'bronze.subscriptions': {
        'file': 'subscriptions.parquet',
        'columns': {
            'workspace_id': 'TEXT NOT NULL',
            'month_start': 'DATE NOT NULL',
            'plan_tier': 'TEXT NOT NULL',
            'billed_seats': 'INTEGER NOT NULL',
            'seat_price_usd': 'NUMERIC(8, 2) NOT NULL',
            'mrr_usd': 'NUMERIC(12, 2) NOT NULL',
        },
        'primary_key': ('workspace_id', 'month_start'),
    },
    'bronze.nps_responses': {
        'file': 'nps_responses.parquet',
        'columns': {
            'response_id': 'TEXT PRIMARY KEY',
            'user_id': 'TEXT NOT NULL',
            'workspace_id': 'TEXT',
            'response_date': 'DATE NOT NULL',
            'score': 'SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 10)',
        },
    },
    'experiments.experiment_assignments': {
        'file': 'experiment_assignments.parquet',
        'columns': {
            'experiment_id': 'TEXT NOT NULL',
            'user_id': 'TEXT NOT NULL',
            'variant': 'TEXT NOT NULL',
            'assigned_at': 'TIMESTAMPTZ NOT NULL',
        },
    },
}

DATE_TYPES = ('DATE',)


def database_url():
    """Build the SQLAlchemy URL from POSTGRES_* environment variables."""
    password = os.environ.get('POSTGRES_PASSWORD')
    if not password:
        raise RuntimeError(
            'POSTGRES_PASSWORD is not set. Copy .env.example to .env and '
            'export its variables, or pass --conn.'
        )
    return URL.create(
        'postgresql+psycopg2',
        username=os.environ.get('POSTGRES_USER', 'connecthub'),
        password=password,
        host=os.environ.get('POSTGRES_HOST', 'localhost'),
        port=int(os.environ.get('POSTGRES_PORT', '5432')),
        database=os.environ.get('POSTGRES_DB', 'connecthub_analytics'),
    )


def create_tables(engine, tables=TABLES):
    """Create the bronze and experiments schemas and tables if missing.

    A table whose columns differ from its spec (an older layout) is dropped and
    recreated. Every load replaces the table's contents anyway; dependent dbt
    views are dropped with it and rebuilt by the next dbt run.
    """
    with engine.begin() as conn:
        for schema in sorted({name.split('.')[0] for name in tables}):
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema}'))
        for name, spec in tables.items():
            schema, table = name.split('.')
            existing = conn.execute(text(
                'SELECT column_name FROM information_schema.columns '
                'WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position'),
                {'s': schema, 't': table}).scalars().all()
            if existing and existing != list(spec['columns']):
                print(f'Recreating {name}: columns changed')
                conn.execute(text(f'DROP TABLE {name} CASCADE'))
            cols = [f'{col} {ddl}' for col, ddl in spec['columns'].items()]
            if 'primary_key' in spec:
                cols.append(f"PRIMARY KEY ({', '.join(spec['primary_key'])})")
            body = ',\n    '.join(cols)
            conn.execute(text(f'CREATE TABLE IF NOT EXISTS {name} (\n    {body}\n)'))


def prepare_frame(df, columns):
    """Select the DDL columns in order and coerce DATE columns to dates."""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f'Missing columns: {missing}')
    out = df[list(columns)].copy()
    for col, ddl in columns.items():
        if ddl.split()[0] in DATE_TYPES:
            out[col] = pd.to_datetime(out[col]).dt.date
    return out


def copy_frame(engine, df, table_name, columns, chunksize=500_000):
    """Replace the table's contents with df using PostgreSQL COPY."""
    df = prepare_frame(df, columns)
    chunks = (df.iloc[start:start + chunksize] for start in range(0, len(df), chunksize))
    return copy_frames(engine, chunks, table_name, columns)


def copy_frames(engine, frames, table_name, columns):
    """Replace the table's contents with an iterable of DataFrames, in one transaction."""
    raw = engine.raw_connection()
    rows = 0
    try:
        with raw.cursor() as cur:
            cur.execute(f'TRUNCATE {table_name}')
            copy_sql = (f'COPY {table_name} ({", ".join(columns)}) '
                        "FROM STDIN WITH (FORMAT csv, NULL '')")
            for frame in frames:
                buf = io.StringIO()
                prepare_frame(frame, columns).to_csv(buf, index=False, header=False)
                buf.seek(0)
                cur.copy_expert(copy_sql, buf)
                rows += len(frame)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return rows


def copy_parquet(engine, path, table_name, columns, batch_size=500_000):
    """Stream a parquet file into the table batch by batch, so memory stays bounded."""
    batches = pq.ParquetFile(path).iter_batches(batch_size=batch_size, columns=list(columns))
    return copy_frames(engine, (b.to_pandas() for b in batches), table_name, columns)


def ingest_from_parquet(input_path, output_path, event_date=None):
    """Ingest events from parquet file, optionally filtering by date."""
    print(f"Reading events from {input_path}...")
    events = pd.read_parquet(input_path)

    if event_date:
        events['event_date'] = pd.to_datetime(events['event_date'])
        events = events[events['event_date'] == event_date]
        print(f"Filtered to {len(events):,} events for {event_date}")

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    events.to_parquet(output_path, index=False)
    print(f"Wrote {len(events):,} events to {output_path}")
    return events


def ingest_to_postgres(parquet_path, conn_string=None, table_name='bronze.events_raw'):
    """Load one parquet file into one of the known Postgres tables."""
    engine = create_engine(conn_string or database_url())
    spec = TABLES[table_name]
    create_tables(engine, {table_name: spec})
    rows = copy_parquet(engine, parquet_path, table_name, spec['columns'])
    print(f"Loaded {rows:,} rows into {table_name}")
    return rows


def load_all_to_postgres(data_dir='data', conn_string=None):
    """Load all parquet files into Postgres. Missing files leave an empty table."""
    engine = create_engine(conn_string or database_url())
    create_tables(engine)

    loaded = {}
    for table, spec in TABLES.items():
        filepath = os.path.join(data_dir, spec['file'])
        if os.path.exists(filepath):
            rows = copy_parquet(engine, filepath, table, spec['columns'])
            loaded[table] = rows
            print(f"OK   {spec['file']} -> {table} ({rows:,} rows)")
        else:
            print(f"SKIP {filepath} not found; {table} left as-is")
    return loaded


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Ingest events')
    parser.add_argument('--date', type=str, help='Event date (YYYY-MM-DD)')
    parser.add_argument('--load-postgres', action='store_true',
                        help='Load all data into Postgres')
    parser.add_argument('--data-dir', type=str, default='data')
    parser.add_argument('--conn', type=str, default=None,
                        help='SQLAlchemy URL; defaults to POSTGRES_* environment variables')
    args = parser.parse_args()

    if args.load_postgres:
        load_all_to_postgres(data_dir=args.data_dir, conn_string=args.conn)
    else:
        ingest_from_parquet(
            'data/events.parquet',
            'data/ingested/events.parquet',
            event_date=args.date
        )
