"""
Load state shared between steps (ops.load_state).

Incremental dbt models only reprocess the last few days, so they are correct
only if older bronze events did not change. Ingestion records which dataset
it loaded; a full reload of *different* data sets needs_full_refresh, which
the dbt step honours and then clears. Reloading the *same* dataset is skipped.
"""
from sqlalchemy import text

DDL = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.load_state (
    table_name TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    row_count BIGINT NOT NULL,
    needs_full_refresh BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""


def _ensure(conn):
    for stmt in DDL.split(';'):
        conn.execute(text(stmt))


def get(engine, table_name):
    with engine.begin() as conn:
        _ensure(conn)
        row = conn.execute(text('SELECT dataset_id, row_count, needs_full_refresh '
                                'FROM ops.load_state WHERE table_name = :t'),
                           {'t': table_name}).mappings().first()
    return dict(row) if row else None


def record_load(engine, table_name, dataset_id, row_count, history_changed):
    """Record a load; history_changed=True means downstream needs a full refresh."""
    with engine.begin() as conn:
        _ensure(conn)
        conn.execute(text("""
            INSERT INTO ops.load_state (table_name, dataset_id, row_count, needs_full_refresh)
            VALUES (:t, :d, :n, :f)
            ON CONFLICT (table_name) DO UPDATE SET
                dataset_id = EXCLUDED.dataset_id, row_count = EXCLUDED.row_count,
                needs_full_refresh = ops.load_state.needs_full_refresh OR EXCLUDED.needs_full_refresh,
                updated_at = now()"""),
            {'t': table_name, 'd': dataset_id, 'n': row_count, 'f': history_changed})


def needs_full_refresh(engine, table_name='bronze.events_raw'):
    state = get(engine, table_name)
    return state is None or state['needs_full_refresh']


def clear_full_refresh(engine, table_name='bronze.events_raw'):
    with engine.begin() as conn:
        _ensure(conn)
        conn.execute(text('UPDATE ops.load_state SET needs_full_refresh = FALSE '
                          'WHERE table_name = :t'), {'t': table_name})
