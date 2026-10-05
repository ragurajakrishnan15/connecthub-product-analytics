"""Shared response shapes: the data envelope and the problem document."""
from datetime import date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar('T')


class DateRange(BaseModel):
    start: date
    end: date


class ResponseMeta(BaseModel):
    """Context for every data response: what the numbers describe and where they came from."""
    as_of: date | None = Field(description='Last loaded event date the data describes')
    data_start: date | None = Field(description='First date with data in the warehouse')
    data_end: date | None = Field(description='Last date with data in the warehouse')
    effective_range: DateRange | None = Field(
        None, description='Date range actually used, after defaults and clamping')
    filters: dict[str, str | None] = Field(default_factory=dict)
    sources: list[str] = Field(description='Warehouse relations the response was built from')
    data_version: str | None = Field(
        description='run_id of the last fully validated pipeline run (null if none)')
    generated_at: datetime = Field(description='When this response was produced (UTC)')
    definitions: str = Field(description='Where the metric definitions are documented')
    caveats: list[str] = Field(default_factory=list)


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: ResponseMeta


class FieldError(BaseModel):
    loc: list[str]
    msg: str
    type: str


class Problem(BaseModel):
    """RFC 9457 problem document (media type application/problem+json)."""
    type: str = Field(examples=['urn:connecthub:problem:data-not-ready'])
    title: str
    status: int
    detail: str
    instance: str
    request_id: str | None
    errors: list[FieldError] | None = None


def problem_responses(*statuses):
    """`responses=` entries documenting problem bodies for the given status codes."""
    return {s: {'model': Problem, 'content': {'application/problem+json': {}}}
            for s in statuses}
