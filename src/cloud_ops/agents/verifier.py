"""VerifierAgent: Independently validates post-remediation telemetry and triggers rollback if unhealthy."""

from __future__ import annotations

from typing import Any

from cloud_ops.llm.gemini_engine import GeminiReasoningEngine
from cloud_ops.mcp_servers.cloudwatch_mcp import CloudWatchMCPServer
from cloud_ops.mcp_servers.ecs_mcp import ECSMCPServer
from cloud_ops.schemas import VerificationReport, utc_now_iso


class VerifierAgent:
    """Post-remediation verification agent that audits fresh CloudWatch logs/metrics and ECS task health."""

    def __init__(self, gemini_engine: GeminiReasoningEngine | None = None) -> None:
        self.gemini = gemini_engine or GeminiReasoningEngine()

    def verify_remediation(
        self,
        incident_id: str,
        service: str,
        pre_metrics: dict[str, Any],
        cloudwatch_mcp: CloudWatchMCPServer,
        ecs_mcp: ECSMCPServer,
    ) -> VerificationReport:
        """Sample post-remediation telemetry and verify SLA recovery across 4 health checks."""
        post_metrics = cloudwatch_mcp.get_service_metrics(service=service, window_ticks=20)
        post_logs = cloudwatch_mcp.get_recent_logs(service=service, limit=8)
        ecs_state = ecs_mcp.describe_ecs_service(service=service)

        pre_err = float(pre_metrics.get("error_rate_pct", 0.0))
        post_err = float(post_metrics.get("error_rate_pct", 0.0))
        pre_lat = int(pre_metrics.get("p95_latency_ms", 0))
        post_lat = int(post_metrics.get("p95_latency_ms", 0))
        post_rss = int(post_metrics.get("memory_rss_mb", 0))
        ecs_health = str(ecs_state.get("health_status", "HEALTHY"))

        checks: list[dict[str, Any]] = [
            {
                "name": "error_rate_below_1pct",
                "target": "< 1.0%",
                "observed": f"{post_err}%",
                "passed": post_err < 1.0,
            },
            {
                "name": "ecs_service_healthy",
                "target": "HEALTHY",
                "observed": ecs_health,
                "passed": ecs_health == "HEALTHY",
            },
            {
                "name": "p95_latency_within_sla",
                "target": "<= 100ms",
                "observed": f"{post_lat}ms",
                "passed": post_lat <= 100,
            },
            {
                "name": "memory_rss_within_budget",
                "target": "< 435MB",
                "observed": f"{post_rss}MB",
                "passed": post_rss < 435,
            },
        ]

        verified = all(bool(c["passed"]) for c in checks)
        rollback_triggered = not verified

        if verified:
            summary = (
                f"VERIFIED: {service} recovered to healthy baseline on revision "
                f"{ecs_state.get('active_task_definition')}. Error rate dropped from "
                f"{pre_err}% to {post_err}% (< 1% SLA), p95 latency improved from "
                f"{pre_lat}ms to {post_lat}ms, and memory RSS stabilized at {post_rss}MB."
            )
        else:
            summary = (
                f"VERIFICATION FAILED: {service} post-remediation error rate is {post_err}% "
                f"(ECS status: {ecs_health}). Automatic rollback triggered to baseline revision "
                f"{ecs_state.get('baseline_task_definition')}."
            )

        return VerificationReport(
            incident_id=incident_id,
            service=service,
            verified=verified,
            pre_metrics=pre_metrics,
            post_metrics=post_metrics,
            post_logs=post_logs,
            checks=checks,
            rollback_triggered=rollback_triggered,
            summary=summary,
            timestamp=utc_now_iso(),
        )
