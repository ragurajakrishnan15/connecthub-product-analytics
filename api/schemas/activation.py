"""/api/activation."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class FunnelStage(BaseModel):
    stage: Literal['signed_up', 'placed_first_call', 'call_and_ai', 'activated_14d']
    users: int
    rate_of_signups: float | None
    rate_of_previous: float | None = Field(description='Users / users at the previous stage; '
                                                       'null for the first stage or a 0 previous')


class MilestoneRates(BaseModel):
    """Independent milestone rates: each milestone regardless of the others."""
    placed_first_call_rate: float | None
    used_ai_feature_rate: float | None
    invited_team_member_rate: float | None


class MilestoneTiming(BaseModel):
    milestone: Literal['placed_first_call', 'used_ai_feature', 'invited_team_member',
                       'activated_14d']
    users_reached: int = Field(description='Users who reached the milestone within 14 days')
    median_days: int | None = Field(description='Smallest day by which at least half of those '
                                                'users had reached it')
    p75_days: int | None


class ActivationPoint(BaseModel):
    period_start: date
    period_end: date
    is_complete: bool
    signups: int
    activated_14d: int
    activation_rate_14d: float | None


class ActivationData(BaseModel):
    signups: int = Field(description='Signups counted (complete windows unless include_incomplete)')
    excluded_incomplete_signups: int = Field(
        description='Signups in the requested range left out because their 14-day window '
                    'extends past the last loaded day (0 with include_incomplete=true)')
    complete_windows_through: date = Field(
        description='Last signup date whose 14-day window is complete (data_end - 14)')
    activation_rate_14d: float | None
    funnel: list[FunnelStage]
    milestones: MilestoneRates
    time_to_milestone: list[MilestoneTiming]
    granularity: str
    series: list[ActivationPoint]
