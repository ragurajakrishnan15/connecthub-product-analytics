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
            'call_date': 'DATE NOT NULL',
            'call_type': 'TEXT',
            'resolved_by_ai': 'BOOLEAN',
            'handle_time_seconds': 'INTEGER',
            'csat_score': 'NUMERIC(3, 1)',
            'escalated_to_human': 'BOOLEAN',
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
    """Create the bronze and experiments schemas and tables if missing."""
    with engine.begin() as conn:
        for schema in sorted({name.split('.')[0] for name in tables}):
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema}'))
        for name, spec in tables.items():
            cols = ',\n    '.join(f'{col} {ddl}' for col, ddl in spec['columns'].items())
            conn.execute(text(f'CREATE TABLE IF NOT EXISTS {name} (\n    {cols}\n)'))


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
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(f'TRUNCATE {table_name}')
            copy_sql = (f'COPY {table_name} ({", ".join(columns)}) '
                        "FROM STDIN WITH (FORMAT csv, NULL '')")
            for start in range(0, len(df), chunksize):
                buf = io.StringIO()
                df.iloc[start:start + chunksize].to_csv(buf, index=False, header=False)
                buf.seek(0)
                cur.copy_expert(copy_sql, buf)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return len(df)


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
    rows = copy_frame(engine, pd.read_parquet(parquet_path), table_name, spec['columns'])
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
            rows = copy_frame(engine, pd.read_parquet(filepath), table, spec['columns'])
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
