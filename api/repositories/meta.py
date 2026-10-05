"""Queries behind /api/meta: the data window, snapshot date and plan tiers."""
from sqlalchemy import text

SOURCES = ('gold.fct_daily_active_users', 'gold.metrics_product_health',
           'gold.fct_workspace_mrr', 'ops.pipeline_runs')


def data_window(conn):
    """(first, last) event date in the warehouse; (None, None) when empty."""
    row = conn.execute(text(
        'SELECT MIN(event_date), MAX(event_date) FROM gold.fct_daily_active_users')).one()
    return row[0], row[1]


def health_snapshot_date(conn):
    return conn.execute(text(
        'SELECT MAX(snapshot_date) FROM gold.metrics_product_health')).scalar()


def plan_tiers(conn):
    """Plan tiers present in billing, cheapest first, with their seat price."""
    rows = conn.execute(text("""
        SELECT plan_tier, MIN(seat_price_usd) AS seat_price_usd
        FROM gold.fct_workspace_mrr
        GROUP BY plan_tier
        ORDER BY MIN(seat_price_usd), plan_tier""")).all()
    return [{'name': r[0], 'seat_price_usd': float(r[1])} for r in rows]
