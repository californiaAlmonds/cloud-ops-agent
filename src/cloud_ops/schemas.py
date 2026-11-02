"""Typed domain schemas for investigations, remediation proposals, verification, and artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Hypothesis:
    """An investigator hypothesis evaluated against log, metric, and config evidence."""

    id: str
    title: str
    description: str
    status: str  # "CONFIRMED" | "RULED_OUT" | "INVESTIGATING"
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskDefDiff:
    """Comparison between active ECS task definition revision and healthy baseline."""

    service: str
    active_revision: str
    baseline_revision: str
    changed_env: dict[str, dict[str, str]]
    release_change: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InvestigationReport:
    """Structured output of the InvestigatorAgent 4-step diagnostic loop."""

    incident_id: str
    service: str
    scenario: str
    summary: str
    hypotheses: list[Hypothesis]
    root_cause: str
    task_def_diff: TaskDefDiff | None
    matched_past_incident: dict[str, Any] | None
    sample_logs: list[dict[str, Any]]
    metrics_snapshot: dict[str, Any]
    multimodal_insights: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=utc_now_iso)
    steps_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "service": self.service,
            "scenario": self.scenario,
            "summary": self.summary,
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "root_cause": self.root_cause,
            "task_def_diff": self.task_def_diff.to_dict() if self.task_def_diff else None,
            "matched_past_incident": self.matched_past_incident,
            "sample_logs": self.sample_logs,
            "metrics_snapshot": self.metrics_snapshot,
            "multimodal_insights": list(self.multimodal_insights),
            "timestamp": self.timestamp,
            "steps_trace": list(self.steps_trace),
        }


@dataclass
class RemediationProposal:
    """Guardrailed remediation plan produced by RemediatorAgent requiring human approval."""

    proposal_id: str
    incident_id: str
    service: str
    title: str
    action_type: str
    mcp_tool: str
    parameters: dict[str, Any]
    aws_cli_equivalent: str
    blast_radius: str
    rollback_plan: str
    requires_approval: bool = True
    status: str = "PENDING_APPROVAL"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    """Independent post-remediation health verification produced by VerifierAgent."""

    incident_id: str
    service: str
    verified: bool
    pre_metrics: dict[str, Any]
    post_metrics: dict[str, Any]
    post_logs: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    rollback_triggered: bool
    summary: str
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactRecord:
    """Metadata for a generated multimodal operational artifact (.xlsx, .svg, .docx, .md)."""

    name: str
    kind: str  # "excel" | "chart" | "docx" | "markdown"
    path: str
    url: str
    size_bytes: int
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentRecord:
    """End-to-end lifecycle record for an operational incident or change workflow."""

    incident_id: str
    title: str
    service: str
    scenario: str
    status: str  # "INVESTIGATING" | "PENDING_APPROVAL" | "REMEDIATED" | "VERIFIED" | "ROLLED_BACK" | "REJECTED"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    investigation: InvestigationReport | None = None
    proposal: RemediationProposal | None = None
    verification: VerificationReport | None = None
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    audit_trail: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "service": self.service,
            "scenario": self.scenario,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "investigation": self.investigation.to_dict() if self.investigation else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "audit_trail": list(self.audit_trail),
        }
