"""
Warehouse fingerprints: row count + an order-independent hash of every row,
per table/view. Two warehouses with equal fingerprints hold the same data.

Used to prove incremental dbt == full refresh, and that pipeline reruns are
idempotent. Run metadata (schema ops) is excluded.
"""
from sqlalchemy import text

SCHEMAS = ('bronze', 'experiments', 'staging', 'intermediate', 'gold', 'semantic', 'analytics')


def list_relations(conn, schemas=SCHEMAS):
    rows = conn.execute(text(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = ANY(:schemas) ORDER BY 1, 2"), {'schemas': list(schemas)}).all()
    return [f'{s}.{t}' for s, t in rows]


def fingerprint(engine, schemas=SCHEMAS, exclude_columns=None):
    """{relation: (row_count, md5)}. exclude_columns maps relation -> columns to skip."""
    exclude_columns = exclude_columns or {}
    out = {}
    with engine.connect() as conn:
        for rel in list_relations(conn, schemas):
            schema, table = rel.split('.')
            cols = conn.execute(text(
                'SELECT column_name FROM information_schema.columns '
                'WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position'),
                {'s': schema, 't': table}).scalars().all()
            cols = [c for c in cols if c not in exclude_columns.get(rel, ())]
            row = 'ROW(' + ', '.join(f'"{c}"' for c in cols) + ')::text'
            count, digest = conn.execute(text(
                f'SELECT COUNT(*), md5(COALESCE(string_agg(md5({row}), \'\' '
                f'ORDER BY md5({row})), \'\')) FROM {rel}')).one()
            out[rel] = (count, digest)
    return out


def diff(a, b):
    """Relations whose fingerprints differ, or that exist on one side only."""
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
