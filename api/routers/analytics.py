"""
Business metric endpoints (PHASE_4_PLAN.md §5). Routes only parse parameters
and call services; every number comes from the warehouse serving tables.

Accepted query parameters are declared per route: any other parameter is a
400 invalid-parameter problem, a malformed value a 422 validation-error, a
range outside the loaded data a 400 invalid-range.
"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.engine import Connection

from api.db import get_conn
from api.params import DATE_DOC, MONTH_PATTERN, Granularity, PlanTier, RiskTier, allow_query_params
from api.schemas.activation import ActivationData
from api.schemas.adoption import AdoptionData
from api.schemas.common import Envelope, problem_responses
from api.schemas.customer_health import HealthSummary, WorkspacePage
from api.schemas.engagement import EngagementData
from api.schemas.experiments import DecisionCode, ExperimentDetail, ExperimentList, Status
from api.schemas.nps import NpsData
from api.schemas.overview import OverviewData
from api.schemas.retention import CohortMatrix, RetentionData
from api.schemas.revenue import RevenueData
from api.schemas.support import SupportData
from api.security import require_api_key
from api.services import activation, adoption, customer_health, engagement, experiments, nps
from api.services import overview, retention, revenue, support

router = APIRouter(prefix='/api', dependencies=[Depends(require_api_key)])
ERRORS = problem_responses(400, 401, 422, 503, 504)

Start = Query(None, description=DATE_DOC)
End = Query(None, description=DATE_DOC)
Tier = Query(None, description='Billed plan tier filter')
StartMonth = Query(None, pattern=MONTH_PATTERN, description='YYYY-MM')
EndMonth = Query(None, pattern=MONTH_PATTERN, description='YYYY-MM')


def params(*names):
    return [Depends(allow_query_params(*names))]


@router.get('/overview', response_model=Envelope[OverviewData], tags=['overview'],
            dependencies=params(), responses=ERRORS, summary='Headline KPIs')
def get_overview(conn: Connection = Depends(get_conn)):
    """MRR, DAU, week-4 retention, AI feature adoption, 14-day activation, NPS and AI resolution
    rate, each over a stated period ending at the last loaded day and compared with the period
    before; plus health tier counts and experiment decisions."""
    return overview.build(conn)


@router.get('/engagement', response_model=Envelope[EngagementData], tags=['engagement'],
            dependencies=params('start', 'end', 'granularity'), responses=ERRORS,
            summary='DAU / WAU / MAU trend')
def get_engagement(start: date | None = Start, end: date | None = End,
                   granularity: Granularity = 'week', conn: Connection = Depends(get_conn)):
    """Source gold.fct_daily_active_users. Default range: the 365 days ending at the last
    loaded day."""
    return engagement.build(conn, start, end, granularity)


@router.get('/activation', response_model=Envelope[ActivationData], tags=['activation'],
            dependencies=params('start', 'end', 'plan_tier', 'granularity', 'include_incomplete'),
            responses=ERRORS, summary='14-day activation funnel')
def get_activation(start: date | None = Start, end: date | None = End,
                   plan_tier: PlanTier | None = Tier, granularity: Granularity = 'week',
                   include_incomplete: bool = Query(False, description='Include signups whose '
                                                    '14-day window is not complete'),
                   conn: Connection = Depends(get_conn)):
    """Source gold.fct_activation_daily / fct_activation_milestone_days, by signup date.
    Default range: the 90 signup days ending at the last date with a complete 14-day window."""
    return activation.build(conn, start, end, plan_tier, granularity, include_incomplete)


@router.get('/retention', response_model=Envelope[RetentionData], tags=['retention'],
            dependencies=params('as_of', 'cohort_start', 'cohort_end'), responses=ERRORS,
            summary='Pooled weekly retention')
def get_retention(as_of: date | None = Query(None, description='Default: last loaded day'),
                  cohort_start: date | None = Query(None), cohort_end: date | None = Query(None),
                  conn: Connection = Depends(get_conn)):
    """Source gold.fct_retention_cohorts through analytics/retention.py."""
    return retention.retention(conn, as_of, cohort_start, cohort_end)


@router.get('/cohorts', response_model=Envelope[CohortMatrix], tags=['retention'],
            dependencies=params('as_of', 'cohort_start', 'cohort_end', 'weeks'),
            responses=ERRORS, summary='Retention cohort matrix')
def get_cohorts(as_of: date | None = Query(None, description='Default: last loaded day'),
                cohort_start: date | None = Query(None), cohort_end: date | None = Query(None),
                weeks: int = Query(12, ge=1, le=12), conn: Connection = Depends(get_conn)):
    """Complete cells only; an ended week with no activity is 0; an unfinished week is null."""
    return retention.cohorts(conn, as_of, cohort_start, cohort_end, weeks)


@router.get('/revenue', response_model=Envelope[RevenueData], tags=['revenue'],
            dependencies=params('start_month', 'end_month', 'plan_tier', 'group_by'),
            responses=ERRORS, summary='Monthly recurring revenue')
def get_revenue(start_month: str | None = StartMonth, end_month: str | None = EndMonth,
                plan_tier: PlanTier | None = Tier,
                group_by: Literal['none', 'plan_tier'] = 'plan_tier',
                conn: Connection = Depends(get_conn)):
    """Source gold.fct_revenue_monthly. Default: the 12 months ending at the latest month."""
    return revenue.build(conn, start_month, end_month, plan_tier, group_by)


@router.get('/feature-adoption', response_model=Envelope[AdoptionData], tags=['feature-adoption'],
            dependencies=params('features', 'max_day', 'start_month', 'end_month'),
            responses=ERRORS, summary='Feature adoption curves and monthly usage')
def get_feature_adoption(features: list[str] | None = Query(None, max_length=10,
                                                            description='Repeatable'),
                         max_day: int = Query(90, ge=0, le=90),
                         start_month: str | None = StartMonth, end_month: str | None = EndMonth,
                         conn: Connection = Depends(get_conn)):
    """Sources gold.fct_feature_adoption, fct_feature_usage_monthly, fct_activity_monthly."""
    return adoption.build(conn, features, max_day, start_month, end_month)


@router.get('/experiments', response_model=Envelope[ExperimentList], tags=['experiments'],
            dependencies=params('decision', 'status'), responses=ERRORS,
            summary='Experiment portfolio')
def get_experiments(decision: DecisionCode | None = Query(None),
                    status: Status | None = Query(None), conn: Connection = Depends(get_conn)):
    """Registered experiments with their persisted evaluation (analytics.experiment_results)."""
    return experiments.list_experiments(conn, decision, status)


@router.get('/experiments/{experiment_id}', response_model=Envelope[ExperimentDetail],
            tags=['experiments'], dependencies=params(),
            responses=problem_responses(400, 401, 404, 422, 503, 504),
            summary='Experiment readout')
def get_experiment(experiment_id: str = Path(pattern=r'^[a-z0-9_]{1,64}$'),
                   conn: Connection = Depends(get_conn)):
    """Persisted evaluation plus the cumulative activation curve per arm."""
    return experiments.detail(conn, experiment_id)


@router.get('/nps', response_model=Envelope[NpsData], tags=['nps'],
            dependencies=params('start', 'end', 'plan_tier', 'granularity', 'min_responses'),
            responses=ERRORS, summary='Net Promoter Score')
def get_nps(start: date | None = Start, end: date | None = End,
            plan_tier: PlanTier | None = Tier, granularity: Granularity = 'month',
            min_responses: int = Query(30, ge=10, le=1000),
            conn: Connection = Depends(get_conn)):
    """Source gold.fct_nps_daily. Default range: the 90 days ending at the last loaded day."""
    return nps.build(conn, start, end, plan_tier, granularity, min_responses)


@router.get('/support', response_model=Envelope[SupportData], tags=['support'],
            dependencies=params('start', 'end', 'plan_tier', 'call_type', 'granularity'),
            responses=ERRORS, summary='Support tickets and AI-agent performance')
def get_support(start: date | None = Start, end: date | None = End,
                plan_tier: PlanTier | None = Tier,
                call_type: str | None = Query(None, max_length=32, pattern=r'^[a-z_]+$'),
                granularity: Granularity = 'week', conn: Connection = Depends(get_conn)):
    """Sources gold.fct_support_daily, fct_agent_performance_daily. Default range: the 90 days
    ending at the last loaded day."""
    return support.build(conn, start, end, plan_tier, call_type, granularity)


@router.get('/customer-health', response_model=Envelope[HealthSummary], tags=['customer-health'],
            dependencies=params('plan_tier'), responses=ERRORS,
            summary='Health tier distribution')
def get_customer_health(plan_tier: PlanTier | None = Tier, conn: Connection = Depends(get_conn)):
    """Latest snapshot of analytics.workspace_health_scores."""
    return customer_health.summary(conn, plan_tier)


@router.get('/customer-health/workspaces', response_model=Envelope[WorkspacePage],
            tags=['customer-health'],
            dependencies=params('tier', 'plan_tier', 'sort', 'order', 'limit', 'offset'),
            responses=ERRORS, summary='Workspaces by health score (paginated)')
def get_health_workspaces(
        tier: RiskTier | None = Query(None), plan_tier: PlanTier | None = Tier,
        sort: Literal['health_score', 'mrr_usd', 'seat_count', 'active_users_30d'] = 'health_score',
        order: Literal['asc', 'desc'] = 'asc', limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000), conn: Connection = Depends(get_conn)):
    """Sorted by an allow-listed column, then workspace_id (stable pagination)."""
    return customer_health.workspaces(conn, tier, plan_tier, sort, order, limit, offset)
