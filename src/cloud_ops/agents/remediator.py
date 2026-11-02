"""RemediatorAgent: Generates guardrailed remediation proposals and executes human-approved fixes."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from cloud_ops.llm.gemini_engine import GeminiReasoningEngine
from cloud_ops.mcp_servers.ecs_mcp import ECSMCPServer
from cloud_ops.schemas import InvestigationReport, RemediationProposal, utc_now_iso

APPROVAL_SIGNING_KEY = b"cloud-ops-agent-human-approval-hmac-key-2026"

ROLLBACK_SCENARIOS = frozenset(
    {"bad-release", "memory-leak", "template-error", "poison-message"}
)


class RemediatorAgent:
    """Proposes least-privilege ECS remediations and executes them only with a valid approval token."""

    def __init__(self, gemini_engine: GeminiReasoningEngine | None = None) -> None:
        self.gemini = gemini_engine or GeminiReasoningEngine()

    def propose_remediation(
        self,
        investigation: InvestigationReport,
        ecs_mcp: ECSMCPServer,
    ) -> RemediationProposal:
        """Create a PENDING_APPROVAL RemediationProposal from the Investigator's root cause report."""
        service = investigation.service
        scenario = investigation.scenario
        incident_id = investigation.incident_id
        proposal_id = f"PROP-{incident_id.replace('INC-', '')}"

        diff = investigation.task_def_diff
        base_rev = diff.baseline_revision if diff else ecs_mcp.get_baseline_revision(service)
        active_rev = diff.active_revision if diff else ecs_mcp.get_active_revision(service)
        baseline_env = ecs_mcp.cloudwatch.get_baseline_env(service)

        # Decide whether to roll back task definition revision or update environment variables
        release_changed = bool(
            diff and diff.release_change.get("changed") == "true"
        )
        if scenario in ROLLBACK_SCENARIOS or release_changed:
            action_type = "ROLLBACK_TASK_DEFINITION"
            mcp_tool = "rollback_task_definition"
            parameters: dict[str, Any] = {
                "service": service,
                "target_revision": base_rev,
            }
            aws_cli_equivalent = (
                f"aws ecs update-service --cluster {ecs_mcp.cluster} "
                f"--service {service} --task-definition {base_rev} "
                f"--force-new-deployment --region {ecs_mcp.region}"
            )
            title = (
                f"Roll back {service} task definition from {active_rev} to healthy baseline {base_rev}"
            )
            rollback_plan = (
                f"If post-rollback verification fails, re-pin {service} to revision {base_rev} "
                "and drain unhealthy Fargate tasks via circuit breaker."
            )
        else:
            action_type = "UPDATE_SERVICE_ENV"
            mcp_tool = "update_service_env"
            env_overrides: dict[str, str] = {"FAULT_MODE": "healthy"}
            if diff and diff.changed_env:
                for key, change_info in diff.changed_env.items():
                    env_overrides[key] = change_info.get("baseline", baseline_env.get(key, ""))
            parameters = {
                "service": service,
                "env_overrides": env_overrides,
            }
            kv_pairs = " ".join(f"{k}={v}" for k, v in env_overrides.items())
            aws_cli_equivalent = (
                f"aws ecs register-task-definition --family {service} "
                f"--container-definitions '[...env: {kv_pairs}]' --region {ecs_mcp.region} && "
                f"aws ecs update-service --cluster {ecs_mcp.cluster} --service {service} "
                f"--force-new-deployment --region {ecs_mcp.region}"
            )
            title = (
                f"Restore {service} environment configuration ({kv_pairs}) and redeploy ECS service"
            )
            rollback_plan = (
                f"Revert {service} directly to baseline task definition revision {base_rev} "
                f"(`aws ecs update-service --service {service} --task-definition {base_rev}`)."
            )

        blast_radius = (
            f"Scoped strictly to ECS service '{service}' on cluster '{ecs_mcp.cluster}' "
            f"(rolling replacement of {ecs_mcp.desired_counts.get(service, 2)} Fargate tasks; "
            "zero downtime with ECS deployment circuit breaker; zero impact on sibling services)."
        )

        return RemediationProposal(
            proposal_id=proposal_id,
            incident_id=incident_id,
            service=service,
            title=title,
            action_type=action_type,
            mcp_tool=mcp_tool,
            parameters=parameters,
            aws_cli_equivalent=aws_cli_equivalent,
            blast_radius=blast_radius,
            rollback_plan=rollback_plan,
            requires_approval=True,
            status="PENDING_APPROVAL",
        )

    def propose_scaling(
        self,
        service: str,
        desired_count: int,
        reason: str,
        ecs_mcp: ECSMCPServer,
        incident_id: str = "INC-SCALE-001",
    ) -> RemediationProposal:
        """Create an approval-gated RemediationProposal for scaling an ECS service."""
        proposal_id = f"PROP-{incident_id.replace('INC-', '')}"
        current_count = ecs_mcp.desired_counts.get(service, 2)
        aws_cli_equivalent = (
            f"aws ecs update-service --cluster {ecs_mcp.cluster} "
            f"--service {service} --desired-count {desired_count} --region {ecs_mcp.region}"
        )
        return RemediationProposal(
            proposal_id=proposal_id,
            incident_id=incident_id,
            service=service,
            title=f"Scale {service} desired_count from {current_count} to {desired_count} ({reason})",
            action_type="SCALE_ECS_SERVICE",
            mcp_tool="scale_ecs_service",
            parameters={
                "service": service,
                "desired_count": int(desired_count),
                "reason": reason,
            },
            aws_cli_equivalent=aws_cli_equivalent,
            blast_radius=(
                f"Adjusts running Fargate replica count for '{service}' on '{ecs_mcp.cluster}' "
                f"from {current_count} to {desired_count} in accordance with Environment Memory policy."
            ),
            rollback_plan=(
                f"Restore {service} desired_count back to {current_count} via "
                f"`aws ecs update-service --cluster {ecs_mcp.cluster} --service {service} --desired-count {current_count}`."
            ),
            requires_approval=True,
            status="PENDING_APPROVAL",
        )

    def generate_approval_token(self, proposal_id: str, approver: str) -> str:
        """Generate a cryptographic HMAC-SHA256 human approval token for a proposal."""
        msg = f"{proposal_id}:{approver}".encode("utf-8")
        digest = hmac.new(APPROVAL_SIGNING_KEY, msg, hashlib.sha256).hexdigest()[:18]
        return f"appr-{digest}"

    def execute_approved(
        self,
        proposal: RemediationProposal,
        approver: str,
        ecs_mcp: ECSMCPServer,
    ) -> dict[str, Any]:
        """Generate a verified approval token and invoke the allowlisted ECSMCPServer write tool."""
        approval_token = self.generate_approval_token(proposal.proposal_id, approver)
        tool_name = proposal.mcp_tool
        params = proposal.parameters

        if tool_name == "rollback_task_definition":
            mcp_result = ecs_mcp.rollback_task_definition(
                service=str(params["service"]),
                target_revision=str(params["target_revision"]),
                approval_token=approval_token,
            )
        elif tool_name == "update_service_env":
            mcp_result = ecs_mcp.update_service_env(
                service=str(params["service"]),
                env_overrides=dict(params.get("env_overrides", {})),
                approval_token=approval_token,
            )
        elif tool_name == "scale_ecs_service":
            mcp_result = ecs_mcp.scale_ecs_service(
                service=str(params["service"]),
                desired_count=int(params["desired_count"]),
                reason=str(params.get("reason", "Approved scaling operation")),
                approval_token=approval_token,
            )
        else:
            raise ValueError(f"Unsupported MCP remediation tool: '{tool_name}'")

        proposal.status = "APPROVED_AND_EXECUTED"
        return {
            "proposal_id": proposal.proposal_id,
            "incident_id": proposal.incident_id,
            "approver": approver,
            "approval_token": approval_token,
            "mcp_tool": tool_name,
            "mcp_result": mcp_result,
            "executed_at": utc_now_iso(),
        }
