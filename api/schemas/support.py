"""/api/support."""
from datetime import date

from pydantic import BaseModel, Field


class Tickets(BaseModel):
    created: int
    resolved: int
    resolved_to_created_ratio: float | None = Field(
        description='Count ratio, not a per-ticket resolution rate (events have no ticket ID)')
    active_user_days: int = Field(description='Sum of daily active users over the range')
    tickets_per_1k_active_user_days: float | None


class AgentPerformance(BaseModel):
    calls: int = Field(description='AI-handled calls (one per evaluation)')
    ai_resolved: int
    escalated: int
    human_handled: int
    ai_resolution_rate: float | None = Field(description='Resolved by the AI agent without '
                                                         'escalation / AI-handled calls')
    escalation_rate: float | None
    human_handled_share: float | None
    avg_csat: float | None = Field(description='Mean CSAT (1-5) over calls with a score')
    avg_handle_time_seconds: float | None


class CallTypePerformance(AgentPerformance):
    call_type: str


class SupportPoint(BaseModel):
    period_start: date
    period_end: date
    is_complete: bool
    tickets_created: int
    tickets_resolved: int
    calls: int
    ai_resolution_rate: float | None
    avg_csat: float | None


class SupportData(BaseModel):
    tickets: Tickets
    ai_agent: AgentPerformance
    by_call_type: list[CallTypePerformance]
    granularity: str
    series: list[SupportPoint]
