"""/api/nps."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class NpsValue(BaseModel):
    responses: int
    promoters: int
    passives: int
    detractors: int
    nps: float | None = Field(description='100 * (promoters - detractors) / responses, -100..100; '
                                          'null below min_responses or with no responses')
    margin_of_error_95: float | None = Field(description='In NPS points; null when nps is null')
    promoter_share: float | None
    passive_share: float | None
    detractor_share: float | None
    suppressed_reason: Literal['no_responses', 'insufficient_responses'] | None = None


class PlanNps(NpsValue):
    plan_tier: str


class NpsPoint(NpsValue):
    period_start: date
    period_end: date
    is_complete: bool


class NpsData(BaseModel):
    min_responses: int
    summary: NpsValue
    by_plan: list[PlanNps]
    granularity: str
    series: list[NpsPoint]
