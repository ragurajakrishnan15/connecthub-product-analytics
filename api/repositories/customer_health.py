"""analytics.workspace_health_scores (persisted by analytics/health_scoring.py)."""
from sqlalchemy import text

SOURCES = ('analytics.workspace_health_scores',)
BIN_WIDTH = 5
_FILTER = """snapshot_date = :snapshot
  AND (CAST(:plan_tier AS text) IS NULL OR plan_tier = :plan_tier)"""

# Sort keys map to fixed SQL; user input only ever selects a key (allow-list).
SORT_COLUMNS = {'health_score': 'health_score', 'mrr_usd': 'mrr_usd',
                'seat_count': 'seat_count', 'active_users_30d': 'active_users_30d'}
ORDERS = {'asc': 'ASC', 'desc': 'DESC'}
COLUMNS = ('workspace_id', 'workspace_name', 'plan_tier', 'health_score', 'risk_tier',
           'seat_count', 'active_users_30d', 'dau_over_seats_ratio', 'features_adopted_count',
           'used_ai_feature_30d', 'pct_ai_calls_automated', 'nps_score',
           'support_tickets_last_30d', 'mrr_usd', 'mrr_change_usd')


def latest_snapshot(conn):
    return conn.execute(text(
        'SELECT MAX(snapshot_date) FROM analytics.workspace_health_scores')).scalar()


def summary(conn, snapshot, plan_tier):
    return dict(conn.execute(text(f"""
        SELECT COUNT(*) AS workspaces, AVG(health_score)::float AS mean_score
        FROM analytics.workspace_health_scores WHERE {_FILTER}"""),
        {'snapshot': snapshot, 'plan_tier': plan_tier}).mappings().one())


def tiers(conn, snapshot, plan_tier):
    rows = conn.execute(text(f"""
        SELECT risk_tier, COUNT(*) FROM analytics.workspace_health_scores WHERE {_FILTER}
        GROUP BY risk_tier"""), {'snapshot': snapshot, 'plan_tier': plan_tier}).all()
    return {r[0]: r[1] for r in rows}


def histogram(conn, snapshot, plan_tier):
    """Workspaces per right-closed score bin of width BIN_WIDTH: (0,5] -> 1 ... (95,100] -> 20
    (a score of exactly 0 falls in bin 1)."""
    rows = conn.execute(text(f"""
        SELECT GREATEST(CEIL(health_score / :width), 1)::int AS bin, COUNT(*)
        FROM analytics.workspace_health_scores WHERE {_FILTER}
        GROUP BY 1"""), {'snapshot': snapshot, 'plan_tier': plan_tier,
                         'width': BIN_WIDTH}).all()
    return {r[0]: r[1] for r in rows}


def by_plan(conn, snapshot):
    rows = conn.execute(text("""
        SELECT plan_tier, risk_tier, COUNT(*) AS n, SUM(health_score)::float AS score_sum
        FROM analytics.workspace_health_scores WHERE snapshot_date = :snapshot
        GROUP BY plan_tier, risk_tier ORDER BY plan_tier"""), {'snapshot': snapshot}).mappings().all()
    return [dict(r) for r in rows]


def count_workspaces(conn, snapshot, tier, plan_tier):
    return conn.execute(text(f"""
        SELECT COUNT(*) FROM analytics.workspace_health_scores
        WHERE {_FILTER} AND (CAST(:tier AS text) IS NULL OR risk_tier = :tier)"""),
        {'snapshot': snapshot, 'plan_tier': plan_tier, 'tier': tier}).scalar()


def workspaces(conn, snapshot, tier, plan_tier, sort, order, limit, offset):
    order_by = f'{SORT_COLUMNS[sort]} {ORDERS[order]} NULLS LAST, workspace_id ASC'
    rows = conn.execute(text(f"""
        SELECT {', '.join(COLUMNS)} FROM analytics.workspace_health_scores
        WHERE {_FILTER} AND (CAST(:tier AS text) IS NULL OR risk_tier = :tier)
        ORDER BY {order_by}
        LIMIT :limit OFFSET :offset"""),
        {'snapshot': snapshot, 'plan_tier': plan_tier, 'tier': tier, 'limit': limit,
         'offset': offset}).mappings().all()
    return [dict(r) for r in rows]
