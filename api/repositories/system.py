"""Catalog and run-history queries used by readiness and metadata."""
import time

from sqlalchemy import text

# Every relation the API reads (PHASE_4_PLAN.md §5). Readiness requires all of
# them to exist and be readable by the API role.
REQUIRED_RELATIONS = (
    'gold.fct_daily_active_users',
    'gold.fct_retention_cohorts',
    'gold.fct_feature_adoption',
    'gold.fct_workspace_mrr',
    'gold.metrics_product_health',
    'gold.fct_activation_daily',
    'gold.fct_activation_milestone_days',
    'gold.fct_revenue_monthly',
    'gold.fct_nps_daily',
    'gold.fct_support_daily',
    'gold.fct_agent_performance_daily',
    'gold.fct_feature_usage_monthly',
    'gold.fct_activity_monthly',
    'gold.fct_experiment_activation_curve',
    'analytics.workspace_health_scores',
    'analytics.experiment_results',
    'ops.pipeline_runs',
)
# A run is "fully validated" once its last step has succeeded.
FINAL_STEP = 'validate_analytics'


def ping(conn):
    """Round-trip time of SELECT 1, in milliseconds."""
    started = time.perf_counter()
    conn.execute(text('SELECT 1'))
    return round((time.perf_counter() - started) * 1000, 2)


def relation_status(conn, relations=REQUIRED_RELATIONS):
    """(missing, not_readable) among `relations`, from the system catalog.

    Uses pg_class/pg_namespace (readable by every role) rather than querying
    the tables, so a missing grant is reported instead of raising.
    """
    rows = conn.execute(text("""
        SELECT r.rel,
               c.oid IS NOT NULL AS present,
               c.oid IS NOT NULL
                   AND has_schema_privilege(n.oid, 'USAGE')
                   AND has_table_privilege(c.oid, 'SELECT') AS readable
        FROM unnest(CAST(:rels AS text[])) AS r(rel)
        LEFT JOIN pg_namespace n ON n.nspname = split_part(r.rel, '.', 1)
        LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = split_part(r.rel, '.', 2)
        ORDER BY r.rel"""), {'rels': list(relations)}).all()
    missing = [rel for rel, present, _ in rows if not present]
    not_readable = [rel for rel, present, readable in rows if present and not readable]
    return missing, not_readable


def last_successful_run(conn):
    """The most recent pipeline run whose final step succeeded, or None."""
    row = conn.execute(text("""
        WITH last_run AS (
            SELECT run_id FROM ops.pipeline_runs
            WHERE step = :final AND status = 'success'
            ORDER BY started_at DESC
            LIMIT 1
        )
        SELECT p.run_id,
               MIN(p.started_at) AS started_at,
               MAX(p.started_at + make_interval(secs => COALESCE(p.duration_s, 0))) AS finished_at,
               SUM(p.duration_s) AS duration_s
        FROM ops.pipeline_runs p
        JOIN last_run USING (run_id)
        GROUP BY p.run_id"""), {'final': FINAL_STEP}).mappings().first()
    return dict(row) if row else None
