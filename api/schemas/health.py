"""Liveness and readiness bodies."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Liveness(BaseModel):
    status: Literal['ok']
    version: str
    uptime_s: float


class DatabaseCheck(BaseModel):
    ok: bool
    latency_ms: float | None = None
    error: str | None = Field(None, description='Problem slug when the check failed')


class RelationsCheck(BaseModel):
    ok: bool
    required: int
    missing: list[str] = Field(description='Required relations that do not exist')
    not_readable: list[str] = Field(description='Relations the API role cannot SELECT')


class PipelineCheck(BaseModel):
    ok: bool
    last_success_run_id: str | None
    last_success_at: datetime | None


class ReadinessChecks(BaseModel):
    database: DatabaseCheck
    relations: RelationsCheck | None
    pipeline: PipelineCheck | None


class Readiness(BaseModel):
    status: Literal['ready', 'not_ready']
    checks: ReadinessChecks
    data_end: date | None
