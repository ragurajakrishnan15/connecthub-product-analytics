"""/api/overview."""
from datetime import date

from pydantic import BaseModel

from api.schemas.common import Kpi


class Kpis(BaseModel):
    mrr_usd: Kpi
    dau: Kpi
    week4_retention_rate: Kpi
    ai_feature_adoption_rate: Kpi
    activation_rate_14d: Kpi
    nps: Kpi
    ai_resolution_rate: Kpi


class TierShare(BaseModel):
    workspaces: int
    share: float | None


class OverviewHealth(BaseModel):
    snapshot_date: date | None
    workspaces: int
    tiers: dict[str, TierShare]


class OverviewExperiments(BaseModel):
    registered: int
    evaluated: int
    by_decision: dict[str, int]


class OverviewData(BaseModel):
    kpis: Kpis
    health: OverviewHealth
    experiments: OverviewExperiments
