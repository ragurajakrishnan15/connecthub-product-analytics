"""/api/experiments and /api/experiments/{experiment_id}."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Status = Literal['running', 'completed', 'not_evaluated']
DecisionCode = Literal['SHIP', 'CONTINUE', 'HOLD', 'REVERT']


class ExperimentInfo(BaseModel):
    experiment_id: str
    kind: Literal['ab', 'aa']
    description: str
    hypothesis: str
    eligibility: str
    start: date
    end: date
    traffic_share: float


class PrimaryHeadline(BaseModel):
    metric: Literal['activated_14d'] = 'activated_14d'
    control_rate: float
    treatment_rate: float
    relative_lift: float
    p_value: float
    significant: bool


class SrmHeadline(BaseModel):
    p_value: float
    detected: bool


class ExperimentSummary(BaseModel):
    experiment: ExperimentInfo
    status: Status = Field(description='completed when the experiment end <= last loaded day, '
                                       'running before that; not_evaluated with no persisted result')
    decision_code: DecisionCode | None
    decision_text: str | None
    primary: PrimaryHeadline | None
    srm: SrmHeadline | None
    sample: dict[str, int] | None = Field(description='Assigned users per arm')


class ExperimentList(BaseModel):
    experiments: list[ExperimentSummary]


class Srm(BaseModel):
    control_count: int
    treatment_count: int
    actual_ratio: float
    p_value: float
    detected: bool


class Primary(BaseModel):
    metric: Literal['activated_14d'] = 'activated_14d'
    control_rate: float
    treatment_rate: float
    absolute_diff: float
    relative_lift: float
    ci_lower: float = Field(description='95% CI of the absolute difference')
    ci_upper: float
    z_statistic: float
    p_value: float
    significant: bool


class Bayesian(BaseModel):
    prob_treatment_better: float
    expected_lift: float
    lift_ci_95: list[float]
    expected_loss: float


class Guardrail(BaseModel):
    metric: Literal['avg_session_minutes_14d', 'revenue_60d']
    control_mean: float
    treatment_mean: float
    absolute_diff: float
    relative_lift: float
    p_value: float
    significant: bool
    failed: bool = Field(description='Significant decrease (blocks shipping)')


class Evaluation(BaseModel):
    srm: Srm
    primary: Primary
    bayesian: Bayesian
    guardrails: list[Guardrail]
    sample_sizes: dict[str, int]
    decision_code: DecisionCode
    decision_text: str


class CurvePoint(BaseModel):
    day: int
    activation_rate: float
    activated: int
    users_in_window: int


class ArmCurve(BaseModel):
    variant: str
    points: list[CurvePoint]


class ExperimentDetail(BaseModel):
    experiment: ExperimentInfo
    status: Status
    evaluation: Evaluation | None
    activation_curve: list[ArmCurve]
