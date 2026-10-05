"""/api/revenue."""
from datetime import date

from pydantic import BaseModel, Field


class Movement(BaseModel):
    new_usd: float
    expansion_usd: float
    reactivation_usd: float
    contraction_usd: float = Field(description='<= 0')
    churned_usd: float = Field(description='<= 0')
    net_new_usd: float = Field(description='Sum of the components = MRR - previous MRR')


class PlanRevenue(BaseModel):
    mrr_usd: float
    paying_workspaces: int
    share_of_mrr: float | None
    arpa_usd: float | None


class RevenueMonth(BaseModel):
    month: date
    is_complete: bool = Field(description='False while the month extends past the last loaded day')
    mrr_usd: float
    previous_mrr_usd: float = Field(description="Last month's MRR of the workspaces counted here")
    mrr_growth_rate: float | None = Field(description='(MRR - previous MRR) / previous MRR')
    paying_workspaces: int
    arpa_usd: float | None = Field(description='MRR / paying workspaces (revenue per paying '
                                               'workspace); null with no paying workspace')
    billed_seats: int
    movement: Movement
    by_plan: dict[str, PlanRevenue] | None = None


class RevenueData(BaseModel):
    latest: RevenueMonth | None = Field(description='Last month of the range (may be incomplete)')
    latest_complete: RevenueMonth | None = Field(description='Last complete month of the range')
    series: list[RevenueMonth]
