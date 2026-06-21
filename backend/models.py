from typing import Any, Literal

from pydantic import BaseModel, Field


class Location(BaseModel):
    id: str
    name: str
    x: float
    y: float
    type: str
    risk_weight: int = 1
    notes: str = ""


class SiteZone(BaseModel):
    id: str
    name: str
    points: list[tuple[float, float]]
    risk: Literal["low", "medium", "high"]


class Event(BaseModel):
    id: str
    timestamp: str
    type: str
    location_id: str
    severity: Literal["info", "warning", "critical"]
    description: str
    actor: str | None = None
    confidence: float = 0.5


class DronePatrol(BaseModel):
    id: str
    started_at: str
    ended_at: str
    route: list[str]
    observations: list[str]
    coverage_quality: Literal["low", "medium", "high"]


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: Any
    rationale: str


class ReasoningStep(BaseModel):
    kind: Literal["thought", "tool", "observation", "summary"]
    text: str
    tool_call: ToolCall | None = None


class Finding(BaseModel):
    id: str
    title: str
    classification: Literal["CLEARED", "NEEDS_REVIEW", "ESCALATE"]
    severity: Literal["info", "warning", "critical"]
    confidence: int
    review_status: Literal["pending", "confirmed", "disputed", "cleared"] = "pending"
    summary: str
    evidence_event_ids: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(default_factory=list)
    uncertainty: str
    supports_escalation: list[str] = Field(default_factory=list)
    supports_false_alarm: list[str] = Field(default_factory=list)


class FollowUpMission(BaseModel):
    title: str
    reason: str
    route: list[str]
    eta_minutes: int
    priority: Literal["low", "medium", "high"]


class Investigation(BaseModel):
    status: Literal["draft", "approved", "rejected"]
    generated_at: str
    headline: str
    confidence: int
    escalation_level: Literal["monitor", "review", "escalate"]
    summary: str
    harmless: list[str]
    needs_escalation: list[str]
    drone_checked: list[str]
    open_questions: list[str]
    findings: list[Finding]
    follow_up_mission: FollowUpMission
    tool_calls: list[ToolCall]
    reasoning_steps: list[ReasoningStep] = Field(default_factory=list)
    coverage_gap: dict[str, Any]
    review_note: str | None = None


class FindingReview(BaseModel):
    finding_id: str
    status: Literal["confirmed", "disputed", "pending", "cleared"]
    note: str = ""


class ReviewRequest(BaseModel):
    note: str = ""
    action: Literal["refine", "approve", "reject"]
    finding_reviews: list[FindingReview] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
