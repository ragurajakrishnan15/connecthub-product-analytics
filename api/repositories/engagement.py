"""gold.fct_daily_active_users (one row per calendar day)."""
from sqlalchemy import text

SOURCES = ('gold.fct_daily_active_users',)


def periods(conn, start, end, granularity):
    rows = conn.execute(text("""
        SELECT DATE_TRUNC(CAST(:grain AS text), event_date)::date AS period_start,
               COUNT(*) AS days,
               AVG(dau)::float AS avg_dau,
               AVG(active_workspaces)::float AS avg_active_workspaces,
               (ARRAY_AGG(wau_7d ORDER BY event_date DESC))[1]::bigint AS wau_7d_end,
               (ARRAY_AGG(mau_28d ORDER BY event_date DESC))[1]::bigint AS mau_28d_end,
               AVG(dau::numeric / NULLIF(mau_28d, 0))::float AS avg_stickiness_rate,
               MAX(event_date) AS last_day
        FROM gold.fct_daily_active_users
        WHERE event_date BETWEEN :start AND :end
        GROUP BY 1
        ORDER BY 1"""), {'grain': granularity, 'start': start, 'end': end}).mappings().all()
    return {r['period_start']: dict(r) for r in rows}


def day(conn, d):
    row = conn.execute(text("""
        SELECT dau, wau_7d::bigint AS wau_7d, mau_28d::bigint AS mau_28d, active_workspaces
        FROM gold.fct_daily_active_users WHERE event_date = :d"""), {'d': d}).mappings().first()
    return dict(row) if row else None
