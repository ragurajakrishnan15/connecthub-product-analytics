"""/api/customer-health and /api/customer-health/workspaces."""
from datetime import date

from pydantic import BaseModel, Field


class TierCount(BaseModel):
    tier: str
    min: float
    max: float
    workspaces: int
    share: float | None


class HistogramBin(BaseModel):
    bin_start: float = Field(description='Exclusive (inclusive for the first bin)')
    bin_end: float = Field(description='Inclusive')
    workspaces: int
    tier: str


class PlanHealth(BaseModel):
    plan_tier: str
    workspaces: int
    mean_score: float | None
    tiers: dict[str, int]


class HealthSummary(BaseModel):
    snapshot_date: date
    workspaces: int
    mean_score: float | None
    tiers: list[TierCount]
    histogram: list[HistogramBin]
    by_plan: list[PlanHealth]
    weights: dict[str, float]


class WorkspaceHealth(BaseModel):
    workspace_id: str
    workspace_name: str | None
    plan_tier: str | None
    health_score: float
    risk_tier: str
    seat_count: int | None
    active_users_30d: int | None
    dau_over_seats_ratio: float | None
    features_adopted_count: int | None
    used_ai_feature_30d: bool | None
    pct_ai_calls_automated: float | None
    nps_score: float | None
    support_tickets_last_30d: int | None
    mrr_usd: float | None
    mrr_change_usd: float | None


class WorkspacePage(BaseModel):
    snapshot_date: date
    total: int = Field(description='Workspaces matching the filters')
    limit: int
    offset: int
    next_offset: int | None = Field(description='Offset of the next page; null on the last page')
    sort: str
    order: str
    items: list[WorkspaceHealth]
