"""/api/engagement."""
from datetime import date

from pydantic import BaseModel, Field


class EngagementSummary(BaseModel):
    day: date = Field(description='Last day of the effective range')
    dau: int
    wau_7d: int
    mau_28d: int
    active_workspaces: int
    stickiness_rate: float | None = Field(description='DAU / MAU(28d) on that day; null if MAU is 0')
    mau_window_complete: bool = Field(description='False when the 28-day window starts before the '
                                                  'first loaded day (MAU is then understated)')


class EngagementPoint(BaseModel):
    period_start: date
    period_end: date
    is_complete: bool = Field(description='False if the range cuts the period or it extends '
                                          'past the last loaded day')
    days: int
    avg_dau: float
    avg_active_workspaces: float
    wau_7d_end: int = Field(description='WAU on the last day of the period')
    mau_28d_end: int = Field(description='MAU on the last day of the period')
    avg_stickiness_rate: float | None = Field(description='Mean of daily DAU/MAU over days with MAU > 0')
    mau_window_complete: bool


class EngagementData(BaseModel):
    summary: EngagementSummary
    granularity: str
    points: list[EngagementPoint]
