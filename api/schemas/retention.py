"""/api/retention and /api/cohorts."""
from datetime import date

from pydantic import BaseModel, Field


class PooledWeek(BaseModel):
    week: int
    retention_rate: float | None = Field(description='Active users / cohort users over the '
                                                     'cohorts whose week N has ended; null if none')
    cohorts_included: int
    users_included: int
    active_users: int


class Headline(BaseModel):
    week_1: float | None
    week_4: float | None
    week_8: float | None
    week_12: float | None


class CohortMonth(BaseModel):
    month: date
    cohorts: int
    cohort_users: int
    week_1_rate: float | None
    week_4_rate: float | None


class RetentionData(BaseModel):
    as_of: date
    cohorts: int = Field(description='Cohorts with a complete first week as of as_of')
    users_in_cohorts: int
    pooled_curve: list[PooledWeek]
    headline: Headline
    by_cohort_month: list[CohortMonth]


class CohortCell(BaseModel):
    week: int
    active_users: int
    retention_rate: float | None


class CohortRow(BaseModel):
    cohort_week: date
    cohort_size: int
    cells: list[CohortCell | None] = Field(description='One entry per week 0..weeks; null when '
                                                       'that week has not ended by as_of')


class CohortMatrix(BaseModel):
    as_of: date
    weeks: list[int]
    cohorts: list[CohortRow]
