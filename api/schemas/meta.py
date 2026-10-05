"""/api/meta: what the dashboard needs to configure itself without literals."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class PipelineRun(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    duration_s: float | None = Field(description='Sum of step durations')


class PlanTier(BaseModel):
    name: str
    seat_price_usd: float = Field(description='Monthly price per active seat')


class Feature(BaseModel):
    name: str
    is_ai: bool


class ExperimentSummary(BaseModel):
    experiment_id: str
    kind: Literal['ab', 'aa']
    start: date
    end: date


class HealthTier(BaseModel):
    label: str
    min: float = Field(description='Inclusive lower bound of the health score')
    max: float = Field(description='Upper bound (inclusive for the top tier)')


class Dataset(BaseModel):
    label: str
    synthetic: bool


class MetaData(BaseModel):
    data_start: date | None
    data_end: date | None
    health_snapshot_date: date | None
    last_successful_run: PipelineRun | None
    plan_tiers: list[PlanTier]
    features: list[Feature]
    experiments: list[ExperimentSummary]
    health_tiers: list[HealthTier]
    api_version: str
    dataset: Dataset
