"""Support tickets and AI voice-agent performance
(docs/metric-definitions.md#serving-models-read-by-the-api)."""
from api import metrics
from api.errors import APIError
from api.params import buckets, check_granularity, resolve_dates
from api.repositories import support as repo
from api.schemas.support import (AgentPerformance, CallTypePerformance, SupportData, SupportPoint,
                                 Tickets)
from api.services import context

DEFAULT_DAYS = 90
NO_TICKETS = {'created': 0, 'resolved': 0, 'active_user_days': 0}
NO_CALLS = {'calls': 0, 'ai_resolved': 0, 'escalated': 0, 'human_handled': 0, 'csat_sum': 0.0,
            'csat_count': 0, 'handle_time_seconds_sum': 0, 'handle_time_count': 0}
CAVEATS = ('Events carry no ticket ID, so ticket resolution time and backlog are not available; '
           'resolved_to_created_ratio is a ratio of counts.',
           'AI-agent metrics cover AI-handled calls only; call_type filters only the AI-agent '
           'figures (tickets have no call type).')


def agent_performance(a):
    def mean(total, count):
        return None if not count else round(total / count, 3)
    return {
        **{k: a[k] for k in ('calls', 'ai_resolved', 'escalated', 'human_handled')},
        'ai_resolution_rate': metrics.ratio(a['ai_resolved'], a['calls']),
        'escalation_rate': metrics.ratio(a['escalated'], a['calls']),
        'human_handled_share': metrics.ratio(a['human_handled'], a['calls']),
        'avg_csat': mean(a['csat_sum'], a['csat_count']),
        'avg_handle_time_seconds': mean(a['handle_time_seconds_sum'], a['handle_time_count']),
    }


def build(conn, start, end, plan_tier, call_type, granularity):
    ctx = context.load(conn)
    if call_type is not None:
        known = repo.call_types(conn)
        if call_type not in known:
            raise APIError('invalid-parameter', f"unknown call_type {call_type!r}; valid: "
                                                f"{', '.join(known)}")
    window = resolve_dates(start, end, ctx.data_start, ctx.data_end, DEFAULT_DAYS)
    check_granularity(granularity, window)
    args = (window.start, window.end)

    t = repo.tickets(conn, *args, plan_tier)
    tickets = Tickets(created=t['created'], resolved=t['resolved'],
                      resolved_to_created_ratio=metrics.ratio(t['resolved'], t['created']),
                      active_user_days=t['active_user_days'],
                      tickets_per_1k_active_user_days=metrics.per_thousand(
                          t['created'], t['active_user_days']))
    ticket_rows = repo.ticket_periods(conn, *args, granularity, plan_tier)
    agent_rows = repo.agent_periods(conn, *args, granularity, plan_tier, call_type)
    series = []
    for nominal, cs, ce, complete in buckets(window, granularity, ctx.data_end):
        tr, ar = ticket_rows.get(nominal, NO_TICKETS), agent_rows.get(nominal, NO_CALLS)
        perf = agent_performance(ar)
        series.append(SupportPoint(period_start=cs, period_end=ce, is_complete=complete,
                                   tickets_created=tr['created'], tickets_resolved=tr['resolved'],
                                   calls=ar['calls'], ai_resolution_rate=perf['ai_resolution_rate'],
                                   avg_csat=perf['avg_csat']))
    data = SupportData(
        tickets=tickets,
        ai_agent=AgentPerformance(**agent_performance(repo.agent(conn, *args, plan_tier,
                                                                 call_type))),
        by_call_type=[CallTypePerformance(call_type=r['call_type'], **agent_performance(r))
                      for r in repo.agent_by_call_type(conn, *args, plan_tier, call_type)],
        granularity=granularity, series=series)
    return context.envelope(
        SupportData, data, ctx, repo.SOURCES, window=window,
        filters={'plan_tier': plan_tier, 'call_type': call_type, 'granularity': granularity},
        caveats=list(CAVEATS), anchor='serving-models-read-by-the-api')
