"""gold.fct_support_daily and gold.fct_agent_performance_daily (additive daily tables)."""
from sqlalchemy import text

SOURCES = ('gold.fct_support_daily', 'gold.fct_agent_performance_daily')
_TICKETS = """COALESCE(SUM(tickets_created), 0)::bigint AS created,
       COALESCE(SUM(tickets_resolved), 0)::bigint AS resolved,
       COALESCE(SUM(active_users), 0)::bigint AS active_user_days"""
_TICKET_FILTER = """event_date BETWEEN :start AND :end
  AND (CAST(:plan_tier AS text) IS NULL OR plan_tier = :plan_tier)"""
_AGENT = """COALESCE(SUM(calls), 0)::bigint AS calls,
       COALESCE(SUM(ai_resolved), 0)::bigint AS ai_resolved,
       COALESCE(SUM(escalated), 0)::bigint AS escalated,
       COALESCE(SUM(human_handled), 0)::bigint AS human_handled,
       COALESCE(SUM(csat_sum), 0)::float AS csat_sum,
       COALESCE(SUM(csat_count), 0)::bigint AS csat_count,
       COALESCE(SUM(handle_time_seconds_sum), 0)::bigint AS handle_time_seconds_sum,
       COALESCE(SUM(handle_time_count), 0)::bigint AS handle_time_count"""
_AGENT_FILTER = """call_date BETWEEN :start AND :end
  AND (CAST(:plan_tier AS text) IS NULL OR plan_tier = :plan_tier)
  AND (CAST(:call_type AS text) IS NULL OR call_type = :call_type)"""


def call_types(conn):
    return conn.execute(text('SELECT DISTINCT call_type FROM gold.fct_agent_performance_daily '
                             'ORDER BY 1')).scalars().all()


def tickets(conn, start, end, plan_tier):
    return dict(conn.execute(text(
        f'SELECT {_TICKETS} FROM gold.fct_support_daily WHERE {_TICKET_FILTER}'),
        {'start': start, 'end': end, 'plan_tier': plan_tier}).mappings().one())


def ticket_periods(conn, start, end, granularity, plan_tier):
    rows = conn.execute(text(f"""
        SELECT DATE_TRUNC(CAST(:grain AS text), event_date)::date AS period_start, {_TICKETS}
        FROM gold.fct_support_daily WHERE {_TICKET_FILTER} GROUP BY 1 ORDER BY 1"""),
        {'start': start, 'end': end, 'plan_tier': plan_tier, 'grain': granularity}
    ).mappings().all()
    return {r['period_start']: dict(r) for r in rows}


def agent(conn, start, end, plan_tier, call_type):
    return dict(conn.execute(text(
        f'SELECT {_AGENT} FROM gold.fct_agent_performance_daily WHERE {_AGENT_FILTER}'),
        {'start': start, 'end': end, 'plan_tier': plan_tier, 'call_type': call_type}
    ).mappings().one())


def agent_by_call_type(conn, start, end, plan_tier, call_type):
    rows = conn.execute(text(f"""
        SELECT call_type, {_AGENT} FROM gold.fct_agent_performance_daily WHERE {_AGENT_FILTER}
        GROUP BY call_type ORDER BY call_type"""),
        {'start': start, 'end': end, 'plan_tier': plan_tier, 'call_type': call_type}
    ).mappings().all()
    return [dict(r) for r in rows]


def agent_periods(conn, start, end, granularity, plan_tier, call_type):
    rows = conn.execute(text(f"""
        SELECT DATE_TRUNC(CAST(:grain AS text), call_date)::date AS period_start, {_AGENT}
        FROM gold.fct_agent_performance_daily WHERE {_AGENT_FILTER} GROUP BY 1 ORDER BY 1"""),
        {'start': start, 'end': end, 'plan_tier': plan_tier, 'call_type': call_type,
         'grain': granularity}).mappings().all()
    return {r['period_start']: dict(r) for r in rows}
