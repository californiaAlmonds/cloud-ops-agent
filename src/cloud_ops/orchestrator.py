"""CloudOpsOrchestrator: Unified multi-agent orchestration facade for CLI and HTTP API."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any

from cloud_ops.agents.investigator import InvestigatorAgent
from cloud_ops.agents.remediator import RemediatorAgent
from cloud_ops.agents.verifier import VerifierAgent
from cloud_ops.config import AgentConfig
from cloud_ops.llm.gemini_engine import GeminiReasoningEngine
from cloud_ops.mcp_servers.aws_cli_guard import DENIED_DESTRUCTIVE_ACTIONS, GuardrailedAWSCLI
from cloud_ops.mcp_servers.cloudwatch_mcp import SUPPORTED_SERVICES, CloudWatchMCPServer
from cloud_ops.mcp_servers.ecs_mcp import ECSMCPServer
from cloud_ops.memory.conversation_store import (
    ChatMessageRecord,
    ConversationSession,
    ConversationStore,
    ExecutedCommandRecord,
)
from cloud_ops.memory.store import MemoryStore
from cloud_ops.multimodal.multimodal_ingest import MultimodalIngestor
from cloud_ops.reporting.chart_builder import (
    generate_metric_chart,
    render_custom_svg_chart,
)
from cloud_ops.reporting.excel_timeline import (
    create_custom_excel_workbook,
    generate_excel_timeline,
)
from cloud_ops.reporting.pir_generator import (
    create_custom_document_report,
    generate_post_incident_report,
)
from cloud_ops.schemas import (
    ArtifactRecord,
    IncidentRecord,
    RemediationProposal,
    VerificationReport,
    utc_now_iso,
)


def _enrich_artifact_dict(art_dict: dict[str, Any]) -> dict[str, Any]:
    """Ensure url, preview_url, and download_url are present on an artifact dictionary."""
    enriched = dict(art_dict)
    url = str(enriched.get("url") or f"/artifacts/{enriched.get('name', '')}")
    enriched["url"] = url
    enriched["preview_url"] = url
    sep = "&" if "?" in url else "?"
    enriched["download_url"] = f"{url}{sep}download=1"
    return enriched


class CloudOpsOrchestrator:
    """Coordinates Investigator, Remediator, Verifier, MCP servers, MemoryStore, and Artifact builders."""

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig.from_env()
        self.config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.config.data_dir.mkdir(parents=True, exist_ok=True)

        self.gemini = GeminiReasoningEngine(
            model=self.config.gemini_model,
            use_vertex_ai=self.config.use_vertex_ai,
            vertex_project=self.config.vertex_project,
            vertex_location=self.config.vertex_location,
        )
        self.cli_guard = GuardrailedAWSCLI()
        self.cloudwatch = CloudWatchMCPServer(
            services_dir=self.config.services_dir,
            live_aws=self.config.live_aws,
            region=self.config.aws_region,
            cli_guard=self.cli_guard,
            cluster=self.config.ecs_cluster,
        )
        self.ecs = ECSMCPServer(
            cloudwatch_mcp=self.cloudwatch,
            cli_guard=self.cli_guard,
            cluster=self.config.ecs_cluster,
            region=self.config.aws_region,
            iam_policy_path=self.config.iam_policy_path,
            live_aws=self.config.live_aws,
        )
        self.memory = MemoryStore(self.config.data_dir / "memory_store.json")
        self.conversation_store = ConversationStore(self.config.data_dir)
        self.ingestor = MultimodalIngestor(self.gemini)

        self.investigator = InvestigatorAgent(
            cloudwatch_mcp=self.cloudwatch,
            ecs_mcp=self.ecs,
            memory_store=self.memory,
            gemini_engine=self.gemini,
            multimodal_ingestor=self.ingestor,
        )
        self.remediator = RemediatorAgent(gemini_engine=self.gemini)
        self.verifier = VerifierAgent(gemini_engine=self.gemini)

        self.incidents: dict[str, IncidentRecord] = {}
        self.active_incident_id: str | None = None
        self._incident_seq = 1100
        self._rehydrate_incidents_from_conversations()

    def _next_incident_id(self) -> str:
        self._incident_seq += 1
        return f"INC-2026-{self._incident_seq:04d}"

    @staticmethod
    def _proposal_from_dict(prop_data: dict[str, Any], fallback_incident_id: str) -> RemediationProposal:
        inc_id = str(prop_data.get("incident_id") or fallback_incident_id)
        return RemediationProposal(
            proposal_id=str(prop_data.get("proposal_id") or f"PROP-{inc_id}"),
            incident_id=inc_id,
            service=str(prop_data.get("service") or "salescreate-api"),
            title=str(prop_data.get("title") or "Proposed AWS Service Change"),
            action_type=str(prop_data.get("action_type") or "ROLLBACK_TASK_DEFINITION"),
            mcp_tool=str(prop_data.get("mcp_tool") or "rollback_task_definition"),
            parameters=dict(prop_data.get("parameters") or {}),
            aws_cli_equivalent=str(prop_data.get("aws_cli_equivalent") or ""),
            blast_radius=str(prop_data.get("blast_radius") or ""),
            rollback_plan=str(prop_data.get("rollback_plan") or ""),
            requires_approval=bool(prop_data.get("requires_approval", True)),
            status=str(prop_data.get("status") or "PENDING_APPROVAL"),
        )

    def _rehydrate_incidents_from_conversations(self) -> None:
        """Restore IncidentRecord entries from persisted conversations so Approve/Cancel survive restarts."""
        seq_re = re.compile(r"INC-\d{4}-(\d+)")
        for sess in self.conversation_store._sessions.values():
            for msg in sess.messages:
                inc_id = msg.incident_id or (msg.proposal or {}).get("incident_id")
                if not inc_id:
                    continue
                m = seq_re.search(str(inc_id))
                if m:
                    self._incident_seq = max(self._incident_seq, int(m.group(1)))
                if isinstance(msg.proposal, dict):
                    prop = self._proposal_from_dict(msg.proposal, str(inc_id))
                    existing = self.incidents.get(str(inc_id))
                    if existing is None or prop.status == "PENDING_APPROVAL":
                        svc = prop.service or sess.active_service or "salescreate-api"
                        self.incidents[str(inc_id)] = IncidentRecord(
                            incident_id=str(inc_id),
                            title=prop.title,
                            service=svc,
                            scenario=self.cloudwatch.get_service_scenario(svc),
                            status=prop.status,
                            created_at=msg.created_at or sess.created_at,
                            updated_at=sess.updated_at,
                            investigation=None,
                            proposal=prop,
                            verification=None,
                            artifacts=[],
                            audit_trail=[],
                        )
                        if self.active_incident_id is None:
                            self.active_incident_id = str(inc_id)

    def _resolve_incident(
        self,
        incident_id: str,
        conversation_id: str | None = None,
    ) -> IncidentRecord:
        """Resolve an IncidentRecord from memory or persisted conversation messages."""
        if conversation_id:
            sess = self.conversation_store.get_conversation(conversation_id)
            if sess is not None:
                for msg in reversed(sess.messages):
                    msg_inc_id = msg.incident_id or (msg.proposal or {}).get("incident_id")
                    if msg_inc_id == incident_id and isinstance(msg.proposal, dict):
                        prop = self._proposal_from_dict(msg.proposal, incident_id)
                        existing = self.incidents.get(incident_id)
                        if existing is not None:
                            existing.proposal = prop
                            existing.service = prop.service or existing.service
                            return existing
                        reconstructed = IncidentRecord(
                            incident_id=incident_id,
                            title=prop.title,
                            service=prop.service or sess.active_service or "salescreate-api",
                            scenario=self.cloudwatch.get_service_scenario(
                                prop.service or sess.active_service or "salescreate-api"
                            ),
                            status=prop.status,
                            created_at=msg.created_at or sess.created_at,
                            updated_at=sess.updated_at,
                            investigation=None,
                            proposal=prop,
                            verification=None,
                            artifacts=[],
                            audit_trail=[],
                        )
                        self.incidents[incident_id] = reconstructed
                        return reconstructed

        if incident_id in self.incidents:
            return self.incidents[incident_id]

        # Scan all persisted conversations for matching incident_id or proposal_id
        for sess in self.conversation_store._sessions.values():
            for msg in reversed(sess.messages):
                prop_dict = msg.proposal if isinstance(msg.proposal, dict) else {}
                if (
                    msg.incident_id == incident_id
                    or prop_dict.get("incident_id") == incident_id
                    or prop_dict.get("proposal_id") == incident_id
                ):
                    prop = self._proposal_from_dict(prop_dict, incident_id)
                    svc = prop.service or sess.active_service or "salescreate-api"
                    reconstructed = IncidentRecord(
                        incident_id=incident_id,
                        title=prop.title,
                        service=svc,
                        scenario=self.cloudwatch.get_service_scenario(svc),
                        status=prop.status,
                        created_at=msg.created_at or sess.created_at,
                        updated_at=sess.updated_at,
                        investigation=None,
                        proposal=prop,
                        verification=None,
                        artifacts=[],
                        audit_trail=[],
                    )
                    self.incidents[incident_id] = reconstructed
                    return reconstructed

        raise KeyError(f"Unknown incident_id '{incident_id}'")

    # -------------------------------------------------------------------------
    # Core Incident Lifecycle Methods
    # -------------------------------------------------------------------------

    def start_investigation(
        self,
        service: str = "salescreate-api",
        scenario: str | None = None,
        user_query: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> IncidentRecord:
        """Run InvestigatorAgent -> RemediatorAgent and create an IncidentRecord awaiting approval."""
        if scenario is not None:
            self.inject_scenario(service, scenario)

        active_scenario = self.cloudwatch.get_service_scenario(service)
        incident_id = self._next_incident_id()
        now_iso = utc_now_iso()

        investigation = self.investigator.investigate(
            incident_id=incident_id,
            service=service,
            attachments=attachments,
            user_query=user_query,
        )
        proposal = self.remediator.propose_remediation(
            investigation=investigation,
            ecs_mcp=self.ecs,
        )

        title = f"{service} incident: {active_scenario} ({investigation.metrics_snapshot.get('error_rate_pct', 0)}% errors)"
        audit_trail: list[dict[str, Any]] = [
            {
                "timestamp": now_iso,
                "actor": "investigator-agent",
                "step": "investigation_completed",
                "status": "COMPLETED",
                "detail": investigation.summary,
            },
            {
                "timestamp": utc_now_iso(),
                "actor": "remediator-agent",
                "step": "remediation_proposed",
                "status": "PENDING_APPROVAL",
                "detail": f"Proposed {proposal.mcp_tool}: {proposal.title} (requires human approval).",
            },
        ]

        incident = IncidentRecord(
            incident_id=incident_id,
            title=title,
            service=service,
            scenario=active_scenario,
            status="PENDING_APPROVAL",
            created_at=now_iso,
            updated_at=utc_now_iso(),
            investigation=investigation,
            proposal=proposal,
            verification=None,
            artifacts=[],
            audit_trail=audit_trail,
        )
        self.incidents[incident_id] = incident
        self.active_incident_id = incident_id
        return incident

    def propose_off_hours_scaling(
        self,
        service: str = "salescreate-api",
        period: str = "off_business_hours",
        free_plan: bool = False,
    ) -> IncidentRecord:
        """Create an approval-gated scaling IncidentRecord using Environment Memory business-hours policy."""
        policy = self.memory.get_scaling_policy(period=period, free_plan=free_plan)
        target_count = int(policy.get("target_desired_count", 1 if period == "off_business_hours" else 2))
        window = policy.get("window", "20:00-07:00 IST")
        reason = f"{period.replace('_', ' ')} policy ({window}): target desired_count={target_count}"

        incident_id = self._next_incident_id()
        now_iso = utc_now_iso()
        proposal = self.remediator.propose_scaling(
            service=service,
            desired_count=target_count,
            reason=reason,
            ecs_mcp=self.ecs,
            incident_id=incident_id,
        )
        investigation = self.investigator.investigate(
            incident_id=incident_id,
            service=service,
            user_query=f"Scale {service} for {period}",
        )
        investigation.summary = (
            f"Consulted Environment Memory business-hours policy for {service}: "
            f"{policy.get('description', reason)} Recommended desired_count={target_count}."
        )

        incident = IncidentRecord(
            incident_id=incident_id,
            title=f"Scheduled Scaling Proposal: {service} -> desired_count={target_count} ({window})",
            service=service,
            scenario=self.cloudwatch.get_service_scenario(service),
            status="PENDING_APPROVAL",
            created_at=now_iso,
            updated_at=now_iso,
            investigation=investigation,
            proposal=proposal,
            verification=None,
            artifacts=[],
            audit_trail=[
                {
                    "timestamp": now_iso,
                    "actor": "environment-memory",
                    "step": "policy_lookup",
                    "status": "COMPLETED",
                    "detail": investigation.summary,
                },
                {
                    "timestamp": now_iso,
                    "actor": "remediator-agent",
                    "step": "scaling_proposed",
                    "status": "PENDING_APPROVAL",
                    "detail": proposal.title,
                },
            ],
        )
        self.incidents[incident_id] = incident
        self.active_incident_id = incident_id
        return incident

    @staticmethod
    def _enrich_artifact_dict(art_dict: dict[str, Any]) -> dict[str, Any]:
        return _enrich_artifact_dict(art_dict)

    @staticmethod
    def _describe_single_cli_command(cmd_str: str, fallback: str) -> str:
        """Produce a specific per-command summary for the CLI Audit Drawer."""
        c = (cmd_str or "").strip()
        if "list-services" in c:
            return "Enumerated active ECS services in cluster."
        if "describe-services" in c:
            m = re.search(r"--services\s+([^\s]+)", c)
            svc_label = m.group(1) if m else "ECS service"
            return f"Inspected live ECS service state, task counts, and active revision for {svc_label}."
        if "describe-task-definition" in c:
            m = re.search(r"--task-definition\s+([^\s]+)", c)
            rev_label = m.group(1) if m else "task definition"
            return f"Inspected container definitions and environment variables for {rev_label}."
        if "filter-log-events" in c:
            m = re.search(r"--log-group-name\s+([^\s]+)", c)
            lg_label = m.group(1) if m else "CloudWatch log group"
            return f"Queried CloudWatch structured log events from {lg_label}."
        if "get-role-policy" in c:
            return "Inspected IAM role policy statements (ReadOnlyDiagnostics, AllowlistedServiceRemediation, ExplicitDenyDestructiveOperations)."
        return fallback

    def _audit_slice_to_records(
        self,
        audit_slice: list[dict[str, Any]],
        tool_name: str,
        output_summary: str,
        start_index: int = 1,
    ) -> list[ExecutedCommandRecord]:
        records: list[ExecutedCommandRecord] = []
        clean_summary = (output_summary or "").strip()[:600]
        multi = len(audit_slice) > 1
        for offset, entry in enumerate(audit_slice):
            allowed = bool(entry.get("allowed", True))
            cat = str(entry.get("category", ""))
            cmd_str = str(entry.get("command", ""))
            if allowed:
                status = "ALLOWED"
                exec_mode = "LIVE_AWS" if self.config.live_aws else "SIMULATION"
                if clean_summary.upper().startswith("BLOCKED"):
                    summary = self._describe_single_cli_command(
                        cmd_str,
                        str(entry.get("reason", "Executed read-only AWS CLI command."))[:600],
                    )
                elif multi and offset < len(audit_slice) - 1:
                    summary = self._describe_single_cli_command(cmd_str, clean_summary)
                else:
                    summary = clean_summary or self._describe_single_cli_command(
                        cmd_str,
                        str(entry.get("reason", ""))[:600],
                    )
            else:
                status = "REQUIRES_APPROVAL" if cat == "WRITE_REQUIRES_APPROVAL" else "BLOCKED"
                exec_mode = "BLOCKED"
                summary = str(entry.get("reason") or clean_summary)[:600]
            records.append(
                ExecutedCommandRecord(
                    index=start_index + offset,
                    tool_name=tool_name,
                    command=cmd_str,
                    status=status,
                    execution_mode=exec_mode,
                    output_summary=summary,
                    timestamp=str(entry.get("timestamp") or utc_now_iso()),
                )
            )
        return records

    # -------------------------------------------------------------------------
    # Conversation Session Management
    # -------------------------------------------------------------------------

    def list_conversations(self) -> dict[str, Any]:
        """Return conversation summaries and active_conversation_id."""
        return {
            "conversations": self.conversation_store.list_conversations(),
            "active_conversation_id": self.conversation_store.active_conversation_id,
        }

    def create_conversation(self, title: str | None = None) -> dict[str, Any]:
        """Create and persist a new ConversationSession."""
        return self.conversation_store.create_conversation(title=title).to_dict()

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        """Look up a full ConversationSession dict by ID."""
        sess = self.conversation_store.get_conversation(conversation_id)
        return sess.to_dict() if sess is not None else None

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a ConversationSession by ID."""
        return self.conversation_store.delete_conversation(conversation_id)

    def approve_remediation(
        self,
        incident_id: str,
        approver: str = "ops-engineer@team",
        conversation_id: str | None = None,
    ) -> IncidentRecord:
        """Execute an approved RemediationProposal, run VerifierAgent, and persist to Incident Memory."""
        incident = self._resolve_incident(incident_id, conversation_id=conversation_id)
        if incident.proposal is None:
            raise ValueError(f"Incident '{incident_id}' has no RemediationProposal to approve.")

        audit_start = len(self.cli_guard.audit_log)
        exec_result = self.remediator.execute_approved(
            proposal=incident.proposal,
            approver=approver,
            ecs_mcp=self.ecs,
        )
        incident.status = "REMEDIATED"
        incident.updated_at = utc_now_iso()
        incident.audit_trail.append(
            {
                "timestamp": incident.updated_at,
                "actor": approver,
                "step": "remediation_approved_and_executed",
                "status": "REMEDIATED",
                "detail": (
                    f"Approved by {approver} (token={exec_result['approval_token']}). "
                    f"Executed {incident.proposal.mcp_tool}: {incident.proposal.aws_cli_equivalent}"
                ),
            }
        )

        if incident.proposal.action_type == "SCALE_ECS_SERVICE":
            ecs_state = self.ecs.describe_ecs_service(incident.service)
            post_m = self.cloudwatch.get_service_metrics(incident.service, window_ticks=10)
            target_cnt = int(incident.proposal.parameters.get("desired_count", 1))
            observed_cnt = int(ecs_state.get("desired_count", 1))
            passed = observed_cnt == target_cnt
            verification = VerificationReport(
                incident_id=incident.incident_id,
                service=incident.service,
                verified=passed,
                pre_metrics=incident.investigation.metrics_snapshot if incident.investigation else post_m,
                post_metrics=post_m,
                post_logs=self.cloudwatch.get_recent_logs(incident.service, limit=5),
                checks=[
                    {
                        "name": "desired_count_updated",
                        "target": str(target_cnt),
                        "observed": str(observed_cnt),
                        "passed": passed,
                    }
                ],
                rollback_triggered=not passed,
                summary=(
                    f"VERIFIED: {incident.service} scaled to desired_count={observed_cnt} "
                    f"(running_count={ecs_state.get('running_count')}) per Environment Memory policy."
                ),
                timestamp=utc_now_iso(),
            )
            incident.verification = verification
            incident.status = "VERIFIED" if passed else "ROLLED_BACK"
            self.memory.scaling_history.append(
                {
                    "incident_id": incident.incident_id,
                    "service": incident.service,
                    "desired_count": observed_cnt,
                    "approver": approver,
                    "timestamp": verification.timestamp,
                }
            )
            self.memory.save()
        else:
            pre_metrics = (
                incident.investigation.metrics_snapshot
                if incident.investigation
                else {"error_rate_pct": 35.0, "p95_latency_ms": 80}
            )
            verification = self.verifier.verify_remediation(
                incident_id=incident.incident_id,
                service=incident.service,
                pre_metrics=pre_metrics,
                cloudwatch_mcp=self.cloudwatch,
                ecs_mcp=self.ecs,
            )
            incident.verification = verification
            incident.status = "VERIFIED" if verification.verified else "ROLLED_BACK"
            self.memory.record_resolved_incident(incident)

        incident.updated_at = utc_now_iso()
        incident.audit_trail.append(
            {
                "timestamp": incident.updated_at,
                "actor": "verifier-agent",
                "step": "post_remediation_verification",
                "status": incident.status,
                "detail": incident.verification.summary,
            }
        )

        # Refresh any existing chart artifact if one was previously requested, without attaching unsolicited files
        if any(a.kind == "chart" for a in incident.artifacts):
            recovery_chart = generate_metric_chart(incident, self.config.artifacts_dir)
            incident.artifacts = [
                recovery_chart if a.kind == "chart" else a for a in incident.artifacts
            ]

        prop_dict = incident.proposal.to_dict()
        ver_dict = incident.verification.to_dict() if incident.verification else None
        matched_conv_id = self.conversation_store.update_proposal_status(
            incident_id=incident.incident_id,
            proposal_dict=prop_dict,
            verification_dict=ver_dict,
            conversation_id=conversation_id,
        )
        target_conv_id = matched_conv_id or conversation_id
        if target_conv_id:
            audit_slice = self.cli_guard.get_audit_slice(audit_start)
            cmd_records = self._audit_slice_to_records(
                audit_slice=audit_slice,
                tool_name=incident.proposal.mcp_tool,
                output_summary=incident.verification.summary if incident.verification else "Executed approved remediation.",
                start_index=1,
            )
            followup_msg = ChatMessageRecord(
                role="assistant",
                content=(
                    f"Remediation executed and verified on `{incident.service}`.\n\n"
                    f"- **Approved By:** `{approver}`\n"
                    f"- **Executed Command:** `{incident.proposal.aws_cli_equivalent}`\n"
                    f"- **Verification Status:** **{incident.status}** — "
                    f"{incident.verification.summary if incident.verification else ''}"
                ),
                executed_commands=cmd_records,
                incident_id=incident.incident_id,
                proposal=None,
                verification=ver_dict,
                artifacts=[],
            )
            self.conversation_store.append_message(
                conversation_id=target_conv_id,
                message=followup_msg,
                active_service=incident.service,
                active_incident_id=incident.incident_id,
            )

        return incident

    def reject_remediation(
        self,
        incident_id: str,
        reason: str = "Rejected by human operator",
        conversation_id: str | None = None,
    ) -> IncidentRecord:
        """Reject a pending remediation proposal without executing any ECS write operations."""
        incident = self._resolve_incident(incident_id, conversation_id=conversation_id)
        incident.status = "REJECTED"
        incident.updated_at = utc_now_iso()
        if incident.proposal is not None:
            incident.proposal.status = "REJECTED"
        incident.audit_trail.append(
            {
                "timestamp": incident.updated_at,
                "actor": "ops-engineer@team",
                "step": "remediation_rejected",
                "status": "REJECTED",
                "detail": reason or "Operator declined proposed remediation.",
            }
        )

        prop_dict = incident.proposal.to_dict() if incident.proposal else {"status": "REJECTED"}
        matched_conv_id = self.conversation_store.update_proposal_status(
            incident_id=incident.incident_id,
            proposal_dict=prop_dict,
            verification_dict=None,
            conversation_id=conversation_id,
        )
        target_conv_id = matched_conv_id or conversation_id
        if target_conv_id:
            followup_msg = ChatMessageRecord(
                role="assistant",
                content=(
                    f"Cancelled proposed change on `{incident.service}`: {reason}. "
                    f"No mutating AWS CLI commands were executed."
                ),
                executed_commands=[],
                incident_id=incident.incident_id,
                proposal=None,
            )
            self.conversation_store.append_message(
                conversation_id=target_conv_id,
                message=followup_msg,
                active_service=incident.service,
                active_incident_id=incident.incident_id,
            )

        return incident

    def generate_artifacts(self, incident_id: str) -> list[ArtifactRecord]:
        """Generate Excel (.xlsx), Chart (.svg), Word (.docx), and Markdown (.md) artifacts for an incident."""
        incident = self._resolve_incident(incident_id)

        excel_art = generate_excel_timeline(incident, self.config.artifacts_dir)
        chart_art = generate_metric_chart(incident, self.config.artifacts_dir)
        docx_art, md_art = generate_post_incident_report(incident, self.config.artifacts_dir)

        generated = [excel_art, chart_art, docx_art, md_art]
        incident.artifacts = generated
        incident.updated_at = utc_now_iso()
        incident.audit_trail.append(
            {
                "timestamp": incident.updated_at,
                "actor": "reporting-engine",
                "step": "multimodal_artifacts_generated",
                "status": "COMPLETED",
                "detail": (
                    f"Generated 4 operational artifacts: {', '.join(a.name for a in generated)}"
                ),
            }
        )
        return generated

    def inject_scenario(self, service: str, scenario: str) -> dict[str, Any]:
        """Switch a service's active fault scenario at runtime (and apply to AWS ECS if live_aws=True)."""
        self.cloudwatch.set_service_scenario(service, scenario)
        if scenario != "healthy":
            base_num = int(self.ecs.get_baseline_revision(service).split(":")[1])
            self.ecs.revision_counters[service] = base_num + 1

        aws_apply_result: dict[str, Any] | None = None
        if self.config.live_aws:
            script_path = self.config.services_dir / service / "scripts" / "scenario.py"
            if script_path.exists():
                cmd = [
                    sys.executable,
                    str(script_path),
                    "apply" if scenario != "healthy" else "reset",
                ]
                if scenario != "healthy":
                    cmd.append(scenario)
                cmd.extend(
                    [
                        "--cluster",
                        self.config.ecs_cluster,
                        "--service",
                        service,
                        "--region",
                        self.config.aws_region,
                    ]
                )
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    aws_apply_result = {
                        "returncode": proc.returncode,
                        "stdout": proc.stdout.strip(),
                        "stderr": proc.stderr.strip(),
                    }
                except Exception as exc:
                    aws_apply_result = {"returncode": -1, "error": str(exc)}

        metrics = self.cloudwatch.get_service_metrics(service, window_ticks=20)
        ecs_info = self.ecs.describe_ecs_service(service)
        return {
            "service": service,
            "scenario": scenario,
            "live_aws": self.config.live_aws,
            "aws_apply_result": aws_apply_result,
            "metrics": metrics,
            "ecs_service": ecs_info,
            "sample_logs": self.cloudwatch.get_recent_logs(service, limit=5),
        }

    def get_dashboard_state(self) -> dict[str, Any]:
        """Return full cluster, services, incidents, memory, IAM, and audit state for UI/CLI."""
        services_dict: dict[str, dict[str, Any]] = {}
        services_list: list[dict[str, Any]] = []

        for svc in SUPPORTED_SERVICES:
            metrics = self.cloudwatch.get_service_metrics(svc, window_ticks=20)
            ecs_info = self.ecs.describe_ecs_service(svc)
            svc_payload = {
                "service": svc,
                "scenario": metrics["scenario"],
                "release": metrics["release"],
                "status": metrics["status"],
                "health_status": ecs_info["health_status"],
                "desired_count": ecs_info["desired_count"],
                "running_count": ecs_info["running_count"],
                "active_revision": ecs_info["active_task_definition"],
                "baseline_revision": ecs_info["baseline_task_definition"],
                "metrics": metrics,
            }
            services_dict[svc] = svc_payload
            services_list.append(svc_payload)

        all_artifacts: list[dict[str, Any]] = []
        for inc in self.incidents.values():
            for art in inc.artifacts:
                all_artifacts.append(_enrich_artifact_dict(art.to_dict()))

        return {
            "cluster": self.config.ecs_cluster,
            "region": self.config.aws_region,
            "live_aws": self.config.live_aws,
            "gemini_model": self.config.gemini_model,
            "llm_status": self.gemini.status_summary(),
            "services": services_dict,
            "services_list": services_list,
            "active_incident_id": self.active_incident_id,
            "incidents": [inc.to_dict() for inc in self.incidents.values()],
            "memory": self.memory.get_summary(),
            "iam_permissions": self.ecs.inspect_iam_role_permissions(),
            "audit_log": list(self.cli_guard.audit_log[-50:]),
            "available_scenarios": self.cloudwatch.list_available_scenarios(),
            "artifacts": all_artifacts,
            "conversations": self.conversation_store.list_conversations(),
            "active_conversation_id": self.conversation_store.active_conversation_id,
        }

    # -------------------------------------------------------------------------
    # Natural Language Chat & Tool-Calling Router
    # -------------------------------------------------------------------------

    def _build_tool_declarations(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_ecs_services",
                "description": (
                    "List all ECS services running in the cluster with task counts, "
                    "health status, and active task definition revisions."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {},
                },
            },
            {
                "name": "investigate_service",
                "description": (
                    "Run a full multi-step read-only diagnostic investigation on an ECS service "
                    "(CloudWatch metrics, logs, ECS service state, task definition diff, and "
                    "Incident Memory correlation) and generate a remediation proposal."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "service": {
                            "type": "STRING",
                            "description": "Name of the ECS service to investigate (e.g., salescreate-api, inventory-worker, notifier, or any deployed service).",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "get_service_logs_and_metrics",
                "description": (
                    "Fetch recent CloudWatch structured logs and metric time-series "
                    "(error_rate_pct, p95_latency_ms, memory_rss_mb, queue_depth) for a service."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "service": {
                            "type": "STRING",
                            "description": "Name of the ECS service.",
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "Maximum number of recent log events to return (default 20, max 25).",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "diff_service_deployments",
                "description": (
                    "Compare the active ECS task definition revision against the previous "
                    "healthy baseline revision (N vs. N-1) for a service."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "service": {
                            "type": "STRING",
                            "description": "Name of the ECS service.",
                        },
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "inspect_iam_and_guardrails",
                "description": (
                    "Inspect the agent's IAM role policy permissions (read-only, allowlisted "
                    "write, and explicit deny boundaries) and optionally validate a test AWS CLI command."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "test_command": {
                            "type": "STRING",
                            "description": "Optional AWS CLI command to validate against GuardrailedAWSCLI.",
                        },
                    },
                },
            },
            {
                "name": "propose_service_remediation",
                "description": (
                    "Prepare a human-approval-gated remediation or scaling proposal "
                    "(rollback_task_definition, update_service_env, or scale_service) without executing immediately."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "service": {
                            "type": "STRING",
                            "description": "Target ECS service name.",
                        },
                        "action_type": {
                            "type": "STRING",
                            "description": "Remediation action type: rollback_task_definition, update_service_env, or scale_service.",
                        },
                        "target_value": {
                            "type": "STRING",
                            "description": "Target revision or desired task count (e.g., '1' or 'salescreate-api:14').",
                        },
                        "reason": {
                            "type": "STRING",
                            "description": "Operational justification for the proposed change.",
                        },
                    },
                    "required": ["service", "action_type"],
                },
            },
            {
                "name": "render_svg_chart",
                "description": (
                    "Render a custom SVG chart when the user explicitly asks for a chart, graph, or plot. "
                    "Pass x_labels and series built from the queried CloudWatch metrics or log timestamps."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {
                            "type": "STRING",
                            "description": "Chart title (e.g. 'na-service-api — Failure Count & P95 Latency').",
                        },
                        "subtitle": {
                            "type": "STRING",
                            "description": "Optional subtitle explaining the data source or time window.",
                        },
                        "service": {
                            "type": "STRING",
                            "description": "Optional service name to auto-populate telemetry if series is omitted.",
                        },
                        "x_labels": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "X-axis timestamp or bucket labels.",
                        },
                        "series": {
                            "type": "ARRAY",
                            "items": {"type": "OBJECT"},
                            "description": "List of series objects: [{'name': 'Error Rate (%)', 'values': [0.2, 18.5], 'unit': '%'}].",
                        },
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "create_excel_report",
                "description": (
                    "Create a custom downloadable .xlsx Excel workbook when the user explicitly asks for "
                    "an Excel spreadsheet, table export, or .xlsx timeline."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {
                            "type": "STRING",
                            "description": "Workbook title.",
                        },
                        "service": {
                            "type": "STRING",
                            "description": "Optional target service name.",
                        },
                        "sheets": {
                            "type": "ARRAY",
                            "items": {"type": "OBJECT"},
                            "description": "List of sheets: [{'name': 'Log Failures', 'headers': ['Timestamp', 'Level', 'Message'], 'rows': [['...', 'ERROR', '...']]}].",
                        },
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "create_document_report",
                "description": (
                    "Create a custom downloadable report (.docx Word document and/or .md Markdown file) "
                    "authored from live CloudWatch logs, metrics, or investigation findings when the user "
                    "explicitly asks for a report or document."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {
                            "type": "STRING",
                            "description": "Report title (e.g. 'CloudWatch Failure Analysis Report — na-service-api').",
                        },
                        "content_markdown": {
                            "type": "STRING",
                            "description": "Full Markdown report content (# headings, ## sections, - bullet points, log citations, and analysis).",
                        },
                        "format": {
                            "type": "STRING",
                            "description": "Output file format: 'docx' (default), 'md', or 'both'.",
                        },
                    },
                    "required": ["title", "content_markdown"],
                },
            },
            {
                "name": "generate_operational_artifacts",
                "description": (
                    "Generate requested operational artifacts (.svg metric chart, .xlsx Excel "
                    "event timeline, .docx Word post-incident report, or .md summary) for a service "
                    "ONLY when the user explicitly requests those specific artifacts."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "service": {
                            "type": "STRING",
                            "description": "Target ECS service name.",
                        },
                        "include_chart": {"type": "BOOLEAN"},
                        "include_excel": {"type": "BOOLEAN"},
                        "include_word": {"type": "BOOLEAN"},
                        "include_markdown": {"type": "BOOLEAN"},
                    },
                    "required": ["service"],
                },
            },
            {
                "name": "run_readonly_aws_cli",
                "description": (
                    "Execute an allowlisted read-only AWS CLI command (aws ecs describe-*, "
                    "aws ecs list-*, aws logs filter-log-events, aws cloudwatch get-*) through GuardrailedAWSCLI."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "command": {
                            "type": "STRING",
                            "description": "The exact AWS CLI command to validate and run.",
                        },
                    },
                    "required": ["command"],
                },
            },
        ]

    def handle_chat(
        self,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Route natural language operator prompts through the multi-turn tool-calling engine."""
        text = (message or "").strip()
        lower = text.lower()
        clean_attachments: list[dict[str, str]] = []
        for att in attachments or []:
            if isinstance(att, dict):
                clean_attachments.append(
                    {
                        "filename": str(att.get("filename") or att.get("name") or "attachment"),
                        "mime_type": str(att.get("mime_type") or att.get("type") or "text/plain"),
                        "preview_text": str(
                            att.get("preview_text") or att.get("content") or ""
                        )[:400],
                    }
                )

        session = self.conversation_store.get_or_create_active(conversation_id)
        user_msg = ChatMessageRecord(
            role="user",
            content=text,
            attachments=clean_attachments,
        )
        session = self.conversation_store.append_message(
            conversation_id=session.conversation_id,
            message=user_msg,
        )

        # Direct chat approval shortcut ("approve" when an incident is active)
        if lower.startswith("approve") and (session.active_incident_id or self.active_incident_id):
            target_inc_id = session.active_incident_id or self.active_incident_id
            assert target_inc_id is not None
            inc = self.approve_remediation(
                target_inc_id,
                conversation_id=session.conversation_id,
            )
            updated_sess = self.conversation_store.get_or_create_active(session.conversation_id)
            last_msg = updated_sess.messages[-1] if updated_sess.messages else user_msg
            reply_str = (
                f"Approved and executed remediation for `{inc.service}`. "
                f"Verifier status: **{inc.status}** — "
                f"{inc.verification.summary if inc.verification else ''}"
            )
            return {
                "reply": reply_str,
                "intent": "approve_remediation",
                "service": inc.service,
                "blocked": False,
                "audit_entry": None,
                "conversation_id": updated_sess.conversation_id,
                "message": last_msg.to_dict(),
                "conversation": updated_sess.to_dict(),
                "incident_id": inc.incident_id,
                "incident": inc.to_dict(),
                "artifacts": [_enrich_artifact_dict(a.to_dict()) for a in inc.artifacts],
                "iam_summary": None,
            }

        executed_commands: list[ExecutedCommandRecord] = []
        turn_incident: IncidentRecord | None = None
        turn_proposal: dict[str, Any] | None = None
        turn_artifacts: list[dict[str, Any]] = []
        turn_service: str | None = session.active_service
        turn_blocked: bool = False
        last_blocked_audit_entry: dict[str, Any] | None = None
        turn_iam_summary: dict[str, Any] | None = None
        turn_scaling_policy: dict[str, Any] | None = None
        primary_intent: str = "chat"

        def add_artifact_Once(art_dict: dict[str, Any]) -> None:
            enriched = _enrich_artifact_dict(art_dict)
            if not any(existing.get("name") == enriched.get("name") for existing in turn_artifacts):
                turn_artifacts.append(enriched)

        def tool_executor(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            nonlocal turn_incident, turn_proposal, turn_service, turn_blocked
            nonlocal last_blocked_audit_entry, turn_iam_summary, turn_scaling_policy, primary_intent

            step_audit_start = len(self.cli_guard.audit_log)
            output_summary = ""
            result_payload: dict[str, Any] = {}

            if tool_name == "list_ecs_services":
                res = self.ecs.list_ecs_services()
                svcs = res.get("services") or []
                output_summary = (
                    f"Listed {len(svcs)} ECS service(s) in {res.get('cluster')}: "
                    + ", ".join(
                        f"{s.get('service')} ({s.get('health_status')}, {s.get('running_count')}/{s.get('desired_count')} tasks)"
                        for s in svcs
                    )
                )
                if primary_intent == "investigate_incident":
                    primary_intent = "list_ecs_services"
                result_payload = res

            elif tool_name == "investigate_service":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                inc = self.start_investigation(
                    service=svc,
                    user_query=text,
                    attachments=attachments,
                )
                turn_incident = inc
                inv = inc.investigation
                err_pct = float((inv.metrics_snapshot if inv else {}).get("error_rate_pct", 0.0) or 0.0)
                is_healthy_svc = inc.scenario == "healthy" and err_pct == 0.0
                is_report_only_request = any(
                    kw in lower for kw in ("report", "excel", "word", "docx", "xlsx", "markdown", "export")
                ) and not any(
                    kw in lower for kw in ("fix", "remediat", "rollback", "roll back", "scale", "resolve")
                )
                if inc.proposal and not is_healthy_svc and not is_report_only_request:
                    turn_proposal = inc.proposal.to_dict()
                output_summary = (
                    f"Investigated {svc}: {inv.summary if inv else inc.title}"
                )
                primary_intent = "investigate_incident"
                inv_llm = inv.to_dict() if inv else None
                if isinstance(inv_llm, dict):
                    inv_llm.pop("incident_id", None)
                    if isinstance(inv_llm.get("matched_past_incident"), dict):
                        inv_llm["matched_past_incident"] = {
                            k: v
                            for k, v in inv_llm["matched_past_incident"].items()
                            if k != "incident_id"
                        }
                prop_llm = dict(turn_proposal) if isinstance(turn_proposal, dict) else None
                if isinstance(prop_llm, dict):
                    prop_llm.pop("proposal_id", None)
                    prop_llm.pop("incident_id", None)
                result_payload = {
                    "service": svc,
                    "status": inc.status,
                    "investigation": inv_llm,
                    "proposal": prop_llm,
                }

            elif tool_name == "get_service_logs_and_metrics":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                limit = min(max(int(args.get("limit") or 20), 1), 25)
                metrics = self.cloudwatch.get_service_metrics(svc, window_ticks=20)
                raw_logs = self.cloudwatch.get_recent_logs(svc, limit=limit)
                compact_logs = [
                    {
                        **entry,
                        "message": str(entry.get("message", ""))[:400],
                    }
                    for entry in raw_logs[:25]
                ]
                if primary_intent == "investigate_incident" and turn_incident is None:
                    primary_intent = "query_logs_and_metrics"
                output_summary = (
                    f"Metrics for {svc}: status={metrics.get('status')}, "
                    f"error_rate={metrics.get('error_rate_pct')}%, "
                    f"p95_latency={metrics.get('p95_latency_ms')}ms; "
                    f"retrieved {len(compact_logs)} log event(s)."
                )
                result_payload = {
                    "service": svc,
                    "metrics": metrics,
                    "logs": compact_logs,
                }

            elif tool_name == "diff_service_deployments":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                diff = self.ecs.diff_task_definitions(svc)
                changed_keys = ", ".join(diff.changed_env.keys()) or "none"
                output_summary = (
                    f"Task definition diff for {svc}: {diff.baseline_revision} -> "
                    f"{diff.active_revision} (changed env keys: {changed_keys})."
                )
                result_payload = {
                    "service": svc,
                    "task_def_diff": diff.to_dict(),
                }

            elif tool_name == "inspect_iam_and_guardrails":
                test_cmd = str(args.get("test_command") or "").strip()
                validation: dict[str, Any] | None = None
                if test_cmd:
                    validation = self.cli_guard.validate_command(
                        test_cmd,
                        actor="user-chat-request",
                    )
                    if not validation.get("allowed"):
                        turn_blocked = True
                        last_blocked_audit_entry = validation
                        primary_intent = "guardrail_blocked"
                perms = self.ecs.inspect_iam_role_permissions()
                turn_iam_summary = perms
                if not turn_blocked:
                    primary_intent = "iam_inspection"
                output_summary = (
                    validation["reason"]
                    if (validation and not validation.get("allowed"))
                    else perms.get("summary", "Inspected IAM role policy and CLI guardrails.")
                )
                result_payload = {
                    "iam_permissions": perms,
                    "validation": validation,
                    "blocked": turn_blocked,
                }

            elif tool_name == "propose_service_remediation":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                action_type = str(args.get("action_type") or "rollback_task_definition").strip().lower()
                target_val = str(args.get("target_value") or "").strip()
                reason = str(args.get("reason") or "Operator requested remediation").strip()
                period = str(args.get("period") or "").strip()
                free_plan = bool(args.get("free_plan", False))

                if "scale" in action_type:
                    if not period:
                        period = (
                            "off_business_hours"
                            if ("off" in reason.lower() or "down" in reason.lower() or "off" in lower)
                            else "business_hours"
                        )
                    if "free" in lower or "zero" in lower or target_val == "0":
                        free_plan = True
                    inc = self.propose_off_hours_scaling(
                        service=svc,
                        period=period,
                        free_plan=free_plan,
                    )
                    if target_val.isdigit() and "business" not in lower and "off" not in lower:
                        custom_cnt = int(target_val)
                        inc.proposal = self.remediator.propose_scaling(
                            service=svc,
                            desired_count=custom_cnt,
                            reason=reason,
                            ecs_mcp=self.ecs,
                            incident_id=inc.incident_id,
                        )
                    turn_scaling_policy = self.memory.get_scaling_policy(
                        period=period,
                        free_plan=free_plan,
                    )
                    primary_intent = "scaling_proposal"
                else:
                    inc = turn_incident or self.start_investigation(
                        service=svc,
                        user_query=reason or text,
                        attachments=attachments,
                    )
                    primary_intent = "investigate_incident"

                turn_incident = inc
                if inc.proposal:
                    turn_proposal = inc.proposal.to_dict()
                output_summary = (
                    f"Prepared approval-gated proposal: "
                    f"{inc.proposal.title if inc.proposal else inc.title}"
                )
                prop_llm = dict(turn_proposal) if isinstance(turn_proposal, dict) else None
                if isinstance(prop_llm, dict):
                    prop_llm.pop("proposal_id", None)
                    prop_llm.pop("incident_id", None)
                result_payload = {
                    "service": svc,
                    "proposal": prop_llm,
                    "scaling_policy": turn_scaling_policy,
                }

            elif tool_name == "render_svg_chart":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                title = str(args.get("title") or f"{svc} — CloudWatch Metric & Log Telemetry").strip()
                subtitle = str(args.get("subtitle") or "").strip()
                x_labels = args.get("x_labels")
                series = args.get("series")
                if not series:
                    # Build series from live CloudWatch logs & metrics for the service
                    metrics = self.cloudwatch.get_service_metrics(svc, window_ticks=12)
                    logs = self.cloudwatch.get_recent_logs(svc, limit=12)
                    x_labels = []
                    err_vals = []
                    lat_vals = []
                    cur_err = float(metrics.get("error_rate_pct", 0.0))
                    cur_lat = float(metrics.get("p95_latency_ms", 120.0))
                    for idx, entry in enumerate(logs[:8]):
                        ts = str(entry.get("timestamp", ""))
                        x_labels.append(ts[11:19] if len(ts) >= 19 else f"T-{8 - idx}m")
                        lvl = str(entry.get("level", "INFO")).upper()
                        msg = str(entry.get("message", ""))
                        if lvl in ("ERROR", "CRITICAL", "FATAL") or "500" in msg or "Exception" in msg or "Error" in msg:
                            err_vals.append(round(max(cur_err, 12.0), 2))
                            lat_vals.append(round(max(cur_lat, 450.0), 1))
                        elif lvl == "WARN":
                            err_vals.append(round(max(cur_err * 0.4, 3.5), 2))
                            lat_vals.append(round(max(cur_lat * 0.6, 240.0), 1))
                        else:
                            err_vals.append(0.2)
                            lat_vals.append(120.0)
                    if not subtitle:
                        subtitle = (
                            f"Service: {svc} | Status: {metrics.get('status', 'UNKNOWN')} | "
                            f"Current Error Rate: {cur_err}% | P95 Latency: {cur_lat}ms"
                        )
                    series = [
                        {"name": "Error Rate (%)", "values": err_vals or [cur_err], "unit": "%", "color": "#ef4444"},
                        {"name": "P95 Latency (ms)", "values": lat_vals or [cur_lat], "unit": "ms", "color": "#38bdf8"},
                    ]
                chart_rec = render_custom_svg_chart(
                    title=title,
                    x_labels=[str(lbl) for lbl in (x_labels or [])],
                    series=series,
                    output_dir=self.config.artifacts_dir,
                    subtitle=subtitle,
                )
                add_artifact_Once(chart_rec.to_dict())
                if turn_incident is not None:
                    turn_incident.artifacts.append(chart_rec)
                primary_intent = "generate_artifacts"
                output_summary = f"Rendered custom SVG chart '{chart_rec.name}' ({title})."
                result_payload = {
                    "service": svc,
                    "artifact": _enrich_artifact_dict(chart_rec.to_dict()),
                }

            elif tool_name == "create_excel_report":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                title = str(args.get("title") or f"{svc} — CloudWatch Log & Metric Export").strip()
                sheets = args.get("sheets")
                if not sheets:
                    metrics = self.cloudwatch.get_service_metrics(svc, window_ticks=15)
                    logs = self.cloudwatch.get_recent_logs(svc, limit=20)
                    log_rows = [
                        [
                            str(entry.get("timestamp", "")),
                            str(entry.get("level", "INFO")),
                            svc,
                            str(entry.get("message", "")),
                        ]
                        for entry in logs
                    ]
                    sheets = [
                        {
                            "name": "CloudWatch Log Events",
                            "headers": ["Timestamp (UTC)", "Level", "Service", "Log Message"],
                            "rows": log_rows,
                        },
                        {
                            "name": "Service Metrics Snapshot",
                            "headers": ["Metric", "Value"],
                            "rows": [
                                ["Service", svc],
                                ["Health Status", str(metrics.get("status", ""))],
                                ["Error Rate (%)", float(metrics.get("error_rate_pct", 0.0))],
                                ["P95 Latency (ms)", float(metrics.get("p95_latency_ms", 0.0))],
                                ["CPU Utilization (%)", float(metrics.get("cpu_utilization_pct", 0.0))],
                                ["Memory Utilization (%)", float(metrics.get("memory_utilization_pct", 0.0))],
                            ],
                        },
                    ]
                excel_rec = create_custom_excel_workbook(
                    title=title,
                    sheets=sheets,
                    output_dir=self.config.artifacts_dir,
                )
                add_artifact_Once(excel_rec.to_dict())
                if turn_incident is not None:
                    turn_incident.artifacts.append(excel_rec)
                primary_intent = "generate_artifacts"
                output_summary = f"Created custom Excel workbook '{excel_rec.name}' ({title})."
                result_payload = {
                    "service": svc,
                    "artifact": _enrich_artifact_dict(excel_rec.to_dict()),
                }

            elif tool_name == "create_document_report":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                title = str(args.get("title") or f"CloudWatch Operational Report — {svc}").strip()
                content_md = str(args.get("content_markdown") or "").strip()
                fmt = str(args.get("format") or "docx").strip().lower()
                if not content_md:
                    metrics = self.cloudwatch.get_service_metrics(svc, window_ticks=15)
                    logs = self.cloudwatch.get_recent_logs(svc, limit=15)
                    lines = [
                        f"# {title}",
                        "",
                        "## Service Telemetry Summary",
                        f"- Service: {svc}",
                        f"- Status: {metrics.get('status')}",
                        f"- Error Rate: {metrics.get('error_rate_pct')}%",
                        f"- P95 Latency: {metrics.get('p95_latency_ms')} ms",
                        "",
                        "## CloudWatch Log Events",
                    ]
                    for entry in logs:
                        lines.append(
                            f"- [{entry.get('timestamp', '')}] {entry.get('level', 'INFO')}: {entry.get('message', '')}"
                        )
                    content_md = "\n".join(lines)
                doc_records = create_custom_document_report(
                    title=title,
                    content_markdown=content_md,
                    output_dir=self.config.artifacts_dir,
                    fmt=fmt,
                )
                for rec in doc_records:
                    add_artifact_Once(rec.to_dict())
                    if turn_incident is not None:
                        turn_incident.artifacts.append(rec)
                primary_intent = "generate_artifacts"
                output_summary = (
                    f"Created custom report document(s) for {svc}: "
                    + ", ".join(r.name for r in doc_records)
                )
                result_payload = {
                    "service": svc,
                    "artifacts": [_enrich_artifact_dict(r.to_dict()) for r in doc_records],
                }

            elif tool_name == "generate_operational_artifacts":
                svc = str(args.get("service") or turn_service or "salescreate-api").strip()
                turn_service = svc
                inc = turn_incident
                if inc is None:
                    if self.active_incident_id and self.active_incident_id in self.incidents:
                        candidate = self.incidents[self.active_incident_id]
                        if candidate.service == svc or not args.get("service"):
                            inc = candidate
                if inc is None:
                    inc = self.start_investigation(
                        service=svc,
                        user_query=text,
                        attachments=attachments,
                    )
                turn_incident = inc

                inc_chart = args.get("include_chart")
                inc_excel = args.get("include_excel")
                inc_word = args.get("include_word")
                inc_md = args.get("include_markdown")
                any_explicit = any(v is True for v in (inc_chart, inc_excel, inc_word, inc_md))
                if not any_explicit:
                    inc_chart = inc_excel = inc_word = inc_md = True

                records: list[ArtifactRecord] = []
                if inc_chart:
                    records.append(generate_metric_chart(inc, self.config.artifacts_dir))
                if inc_excel:
                    records.append(generate_excel_timeline(inc, self.config.artifacts_dir))
                if inc_word or inc_md:
                    docx_rec, md_rec = generate_post_incident_report(inc, self.config.artifacts_dir)
                    if inc_word:
                        records.append(docx_rec)
                    if inc_md:
                        records.append(md_rec)
                inc.artifacts = records
                for rec in records:
                    add_artifact_Once(rec.to_dict())
                primary_intent = "generate_artifacts"
                output_summary = (
                    f"Generated {len(records)} operational artifact(s) for {svc}: "
                    + ", ".join(r.name for r in records)
                )
                result_payload = {
                    "service": svc,
                    "artifacts": [_enrich_artifact_dict(r.to_dict()) for r in records],
                }

            elif tool_name == "run_readonly_aws_cli":
                cmd = str(args.get("command") or "").strip()
                res = self.cli_guard.execute_command(
                    cmd,
                    live_aws=self.config.live_aws,
                    actor="investigator-agent",
                )
                if not res.get("allowed"):
                    turn_blocked = True
                    last_blocked_audit_entry = res
                    primary_intent = "guardrail_blocked"
                output_summary = str(
                    res.get("stdout") or res.get("stderr") or res.get("reason") or ""
                )[:600]
                result_payload = res

            else:
                output_summary = f"Unsupported tool '{tool_name}'"
                result_payload = {"error": output_summary}

            audit_slice = self.cli_guard.get_audit_slice(step_audit_start)
            new_records = self._audit_slice_to_records(
                audit_slice=audit_slice,
                tool_name=tool_name,
                output_summary=output_summary,
                start_index=len(executed_commands) + 1,
            )
            executed_commands.extend(new_records)
            return result_payload

        history_dicts = [m.to_dict() for m in session.messages[-16:]]
        llm_res = self.gemini.chat_with_tools(
            messages=history_dicts,
            attachments=clean_attachments,
            tool_declarations=self._build_tool_declarations(),
            tool_executor=tool_executor,
            active_service=session.active_service,
        )
        reply_text = str(llm_res.get("reply") or "").strip()

        assistant_msg = ChatMessageRecord(
            role="assistant",
            content=reply_text,
            executed_commands=executed_commands,
            incident_id=turn_incident.incident_id if turn_incident else None,
            proposal=turn_proposal,
            verification=(
                turn_incident.verification.to_dict()
                if (turn_incident and turn_incident.verification)
                else None
            ),
            artifacts=turn_artifacts,
        )
        updated_session = self.conversation_store.append_message(
            conversation_id=session.conversation_id,
            message=assistant_msg,
            active_service=turn_service,
            active_incident_id=turn_incident.incident_id if turn_incident else None,
        )

        return {
            "reply": reply_text,
            "intent": primary_intent,
            "service": turn_service,
            "blocked": turn_blocked,
            "audit_entry": last_blocked_audit_entry,
            "validation": last_blocked_audit_entry,
            "iam_permissions": turn_iam_summary,
            "iam_summary": turn_iam_summary,
            "conversation_id": updated_session.conversation_id,
            "message": assistant_msg.to_dict(),
            "conversation": updated_session.to_dict(),
            "incident_id": turn_incident.incident_id if turn_incident else None,
            "incident": turn_incident.to_dict() if turn_incident else None,
            "investigation": (
                turn_incident.investigation.to_dict()
                if (turn_incident and turn_incident.investigation)
                else None
            ),
            "proposal": turn_proposal,
            "scaling_policy": turn_scaling_policy,
            "artifacts": turn_artifacts,
        }

    def _detect_service_in_text(self, lower_text: str, default: str | None = None) -> str:
        svc, explicit = self.gemini._detect_service_from_text(lower_text, active_service=default)
        if explicit or default:
            return svc
        for known_svc in SUPPORTED_SERVICES:
            if self.cloudwatch.get_service_scenario(known_svc) != "healthy":
                return known_svc
        return svc
