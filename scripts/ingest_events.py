"""
Bronze ingestion: load the generated parquet files into PostgreSQL.

Warehouse layout (local PostgreSQL):
    bronze.*        raw tables loaded by this script
    experiments.*   experiment assignments (owned by experimentation.assignment)
    staging / intermediate / gold / semantic   built by dbt (dbt_project/)
    analytics.*     outputs of the Python analytics (health scores, experiment results)

Every load is idempotent:
- full mode replaces each table's contents (TRUNCATE + COPY in one transaction);
  --through limits events to event_date <= that date (for incremental runs);
- partition mode (--start-date/--end-date) deletes that event_date range from
  bronze.events_raw and re-inserts it from parquet, in one transaction, and
  fully replaces the (small) dimension tables.

CLI:
    python scripts/ingest_events.py --load-postgres [--through YYYY-MM-DD]
    python scripts/ingest_events.py --load-postgres --start-date D [--end-date D]
"""
import argparse
import io
import os
import sys

import pandas as pd
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.config import database_url  # noqa: E402,F401  (re-exported for callers)

EVENTS_TABLE = 'bronze.events_raw'

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
        'indexes': ['event_date'],
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
}

DATE_TYPES = ('DATE',)


def create_tables(engine, tables=TABLES):
    """Create the bronze schema and tables if missing.

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
            for col in spec.get('indexes', []):
                conn.execute(text(
                    f'CREATE INDEX IF NOT EXISTS {table}_{col}_idx ON {name} ({col})'))


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


def copy_frames(engine, frames, table_name, columns, before_sql=None, params=None):
    """COPY an iterable of DataFrames into the table in one transaction.

    before_sql runs first in the same transaction (default: TRUNCATE the table).
    """
    raw = engine.raw_connection()
    rows = 0
    try:
        with raw.cursor() as cur:
            cur.execute(before_sql or f'TRUNCATE {table_name}', params)
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


def _batches(path, columns, row_filter=None, batch_size=500_000):
    if row_filter is None:
        batches = pq.ParquetFile(path).iter_batches(batch_size=batch_size, columns=list(columns))
    else:
        batches = ds.dataset(path).to_batches(columns=list(columns), filter=row_filter,
                                              batch_size=batch_size)
    return (b.to_pandas() for b in batches)


def copy_parquet(engine, path, table_name, columns, batch_size=500_000):
    """Stream a parquet file into the table batch by batch, so memory stays bounded."""
    return copy_frames(engine, _batches(path, columns, batch_size=batch_size),
                       table_name, columns)


def _date_filter(start=None, end=None):
    field, cond = ds.field('event_date'), None
    for op, value in (('>=', start), ('<=', end)):
        if value is None:
            continue
        v = pd.Timestamp(value).date()
        c = (field >= v) if op == '>=' else (field <= v)
        cond = c if cond is None else cond & c
    return cond


def load_events(engine, data_dir='data', start=None, end=None, replace_all=True):
    """Load events, optionally limited to [start, end] by event_date.

    replace_all=True truncates the table first (full load); False deletes only
    the [start, end] range (partition load). Both are idempotent.
    """
    spec = TABLES[EVENTS_TABLE]
    path = os.path.join(data_dir, spec['file'])
    if replace_all:
        before, params = f'TRUNCATE {EVENTS_TABLE}', None
    else:
        if start is None or end is None:
            raise ValueError('partition loads need both start and end dates')
        before = f'DELETE FROM {EVENTS_TABLE} WHERE event_date BETWEEN %(s)s AND %(e)s'
        params = {'s': pd.Timestamp(start).date(), 'e': pd.Timestamp(end).date()}
    return copy_frames(engine, _batches(path, spec['columns'], _date_filter(start, end)),
                       EVENTS_TABLE, spec['columns'], before, params)


def load_all_to_postgres(data_dir='data', conn_string=None, through=None,
                         start_date=None, end_date=None, engine=None, skip_tables=()):
    """Load every bronze table. Returns {table: rows loaded}.

    Dimension tables are always fully replaced. Events: full replace (optionally
    only up to `through`), or a partition replace when start_date is given.
    Missing files leave the table as-is.
    """
    engine = engine or create_engine(conn_string or database_url())
    create_tables(engine)

    loaded = {}
    for table, spec in TABLES.items():
        if table in skip_tables:
            continue
        filepath = os.path.join(data_dir, spec['file'])
        if not os.path.exists(filepath):
            print(f'SKIP {filepath} not found; {table} left as-is')
            continue
        if table == EVENTS_TABLE:
            if start_date:
                rows = load_events(engine, data_dir, start_date, end_date or start_date,
                                   replace_all=False)
            else:
                rows = load_events(engine, data_dir, end=through)
        else:
            rows = copy_parquet(engine, filepath, table, spec['columns'])
        loaded[table] = rows
        print(f"OK   {spec['file']} -> {table} ({rows:,} rows)")
    return loaded


def ingest_to_postgres(parquet_path, conn_string=None, table_name='bronze.events_raw'):
    """Load one parquet file into one of the known Postgres tables."""
    engine = create_engine(conn_string or database_url())
    spec = TABLES[table_name]
    create_tables(engine, {table_name: spec})
    rows = copy_parquet(engine, parquet_path, table_name, spec['columns'])
    print(f"Loaded {rows:,} rows into {table_name}")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description='Load generated parquet into bronze.*')
    parser.add_argument('--load-postgres', action='store_true',
                        help='Load all bronze tables into Postgres')
    parser.add_argument('--data-dir', type=str, default='data')
    parser.add_argument('--through', help='Full load of events with event_date <= this date')
    parser.add_argument('--start-date', help='Partition load: replace events from this date')
    parser.add_argument('--end-date', help='Partition load end date (default: --start-date)')
    parser.add_argument('--conn', type=str, default=None,
                        help='SQLAlchemy URL; defaults to POSTGRES_* environment variables')
    args = parser.parse_args(argv)
    if not args.load_postgres:
        parser.error('nothing to do: pass --load-postgres')
    if args.through and args.start_date:
        parser.error('--through and --start-date are mutually exclusive')
    load_all_to_postgres(args.data_dir, args.conn, args.through, args.start_date, args.end_date)
    return 0


if __name__ == '__main__':
    sys.exit(main())
