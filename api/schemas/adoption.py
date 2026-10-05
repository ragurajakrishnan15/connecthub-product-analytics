"""/api/feature-adoption."""
from datetime import date

from pydantic import BaseModel, Field


class CurvePoint(BaseModel):
    day: int
    cumulative_rate: float | None = Field(description='Users who adopted by day D / ALL users '
                                                      '(right-censored: understates later days)')
    observed_rate: float | None = Field(description='Adopters by day D among users with at least '
                                                    'D days of history / those users; null if none')
    eligible_users: int


class Curve(BaseModel):
    feature: str
    is_ai: bool
    total_users: int
    points: list[CurvePoint]


class AdoptionAt(BaseModel):
    feature: str
    day_7: CurvePoint | None
    day_30: CurvePoint | None
    day_90: CurvePoint | None


class FeatureMonth(BaseModel):
    active_workspaces: int
    active_users: int
    workspace_share: float | None = Field(description='Workspaces using the feature / workspaces '
                                                      'using any mapped feature that month')


class UsageMonth(BaseModel):
    month: date
    is_complete: bool
    active_workspaces: int = Field(description='Workspaces with any event')
    feature_active_workspaces: int = Field(description='Workspaces using any mapped feature')
    ai_active_workspaces: int
    ai_adoption_rate: float | None = Field(description='AI-active / feature-active workspaces '
                                                       '(semantic metric ai_feature_adoption)')
    by_feature: dict[str, FeatureMonth]


class AdoptionData(BaseModel):
    curves: list[Curve]
    adoption_at: list[AdoptionAt]
    monthly: list[UsageMonth]
