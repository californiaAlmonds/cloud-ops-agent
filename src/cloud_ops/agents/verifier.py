"""VerifierAgent: Independently validates post-remediation telemetry and reports honestly.

Verification fails closed: missing telemetry, an unfinished ECS rollout or an unreadable service
state counts as a failed check, never as healthy. The verifier does not roll anything back; if
verification fails it says so and leaves the next step to a human-approved proposal.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from cloud_ops.llm.gemini_engine import GeminiReasoningEngine
from cloud_ops.mcp_servers.cloudwatch_mcp import CloudWatchMCPServer
from cloud_ops.mcp_servers.ecs_mcp import ECSMCPServer
from cloud_ops.schemas import VerificationReport, utc_now_iso


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class VerifierAgent:
    """Post-remediation verification agent that audits fresh CloudWatch logs/metrics and ECS task health."""

    MAX_ERROR_RATE_PCT = 1.0
    MAX_P95_LATENCY_MS = 100
    MAX_MEMORY_RSS_MB = 435
    MIN_LOG_EVENTS = 3

    def __init__(
        self,
        gemini_engine: GeminiReasoningEngine | None = None,
        settle_timeout_s: float = 240.0,
        poll_interval_s: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gemini = gemini_engine or GeminiReasoningEngine()
        self.settle_timeout_s = settle_timeout_s
        self.poll_interval_s = poll_interval_s
        self._sleep = sleep
        self._clock = clock

    def _settle(self, service: str, ecs_mcp: ECSMCPServer) -> dict[str, Any]:
        """On real AWS, wait for the ECS rollout to finish before sampling; simulation is instant."""
        state = ecs_mcp.describe_ecs_service(service=service)
        if not getattr(ecs_mcp, "live_aws", False):
            return state
        deadline = self._clock() + self.settle_timeout_s
        while state.get("rollout_state") != "COMPLETED" and self._clock() < deadline:
            self._sleep(self.poll_interval_s)
            state = ecs_mcp.describe_ecs_service(service=service)
        return state

    def verify_remediation(
        self,
        incident_id: str,
        service: str,
        pre_metrics: dict[str, Any],
        cloudwatch_mcp: CloudWatchMCPServer,
        ecs_mcp: ECSMCPServer,
    ) -> VerificationReport:
        """Sample post-remediation telemetry and verify recovery across ECS and log-derived checks."""
        ecs_state = self._settle(service, ecs_mcp)
        post_metrics = cloudwatch_mcp.get_service_metrics(service=service, window_ticks=20)
        post_logs = cloudwatch_mcp.get_recent_logs(service=service, limit=8)

        events = _num(post_metrics.get("log_events", post_metrics.get("sample_size")))
        post_err = _num(post_metrics.get("error_rate_pct"))
        post_lat = _num(post_metrics.get("p95_latency_ms"))
        post_rss = _num(post_metrics.get("memory_rss_mb"))
        desired = _num(ecs_state.get("desired_count"))
        running = _num(ecs_state.get("running_count"))
        ecs_health = str(ecs_state.get("health_status") or "UNKNOWN")
        rollout = str(ecs_state.get("rollout_state") or "UNKNOWN")
        has_telemetry = events is not None and events >= self.MIN_LOG_EVENTS

        def check(name: str, target: str, observed: str, passed: bool) -> dict[str, Any]:
            return {"name": name, "target": target, "observed": observed, "passed": bool(passed)}

        checks = [
            check(
                "telemetry_available",
                f">= {self.MIN_LOG_EVENTS} log events",
                "unavailable" if events is None else f"{int(events)} events",
                has_telemetry,
            ),
            check(
                "error_rate_below_1pct",
                f"< {self.MAX_ERROR_RATE_PCT}%",
                "unavailable" if post_err is None else f"{post_err}%",
                has_telemetry and post_err is not None and post_err < self.MAX_ERROR_RATE_PCT,
            ),
            check("ecs_service_healthy", "HEALTHY", ecs_health, ecs_health == "HEALTHY"),
            check("ecs_rollout_completed", "COMPLETED", rollout, rollout == "COMPLETED"),
            check(
                "running_matches_desired",
                "running == desired",
                f"{running} / {desired}",
                running is not None and desired is not None and running == desired,
            ),
        ]
        # Latency and memory are only judged when the service actually reports them.
        if post_metrics.get("latency_reported", True):
            checks.append(
                check(
                    "p95_latency_within_sla",
                    f"<= {self.MAX_P95_LATENCY_MS}ms",
                    "unavailable" if post_lat is None else f"{int(post_lat)}ms",
                    post_lat is not None and post_lat <= self.MAX_P95_LATENCY_MS,
                )
            )
        if post_metrics.get("memory_reported", True):
            checks.append(
                check(
                    "memory_rss_within_budget",
                    f"< {self.MAX_MEMORY_RSS_MB}MB",
                    "unavailable" if post_rss is None else f"{int(post_rss)}MB",
                    post_rss is not None and post_rss < self.MAX_MEMORY_RSS_MB,
                )
            )

        verified = all(bool(c["passed"]) for c in checks)
        pre_err = pre_metrics.get("error_rate_pct", "n/a")
        pre_lat = pre_metrics.get("p95_latency_ms", "n/a")

        if verified:
            summary = (
                f"VERIFIED: {service} recovered on revision {ecs_state.get('active_task_definition')}. "
                f"Error rate dropped from {pre_err}% to {post_err}% (< {self.MAX_ERROR_RATE_PCT}%), "
                f"p95 latency went from {pre_lat}ms to {int(post_lat or 0)}ms, and the ECS rollout "
                f"is {rollout} with {int(running or 0)}/{int(desired or 0)} tasks running."
            )
        else:
            failed = ", ".join(c["name"] for c in checks if not c["passed"])
            summary = (
                f"VERIFICATION FAILED: {service} did not pass: {failed}. "
                "No automatic rollback was performed; review the failing checks and approve a "
                "new remediation if needed."
            )

        return VerificationReport(
            incident_id=incident_id,
            service=service,
            verified=verified,
            pre_metrics=pre_metrics,
            post_metrics=post_metrics,
            post_logs=post_logs,
            checks=checks,
            rollback_triggered=False,
            summary=summary,
            timestamp=utc_now_iso(),
        )
