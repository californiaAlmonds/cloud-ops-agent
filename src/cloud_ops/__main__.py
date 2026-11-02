"""CLI Entrypoint for Cloud Ops Agent (`python -m cloud_ops serve` and `python -m cloud_ops demo`)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from cloud_ops.api.server import run_server
from cloud_ops.config import AgentConfig
from cloud_ops.orchestrator import CloudOpsOrchestrator


def run_cli_demo(as_json: bool = False, live_aws: bool | None = None) -> dict[str, Any]:
    """Execute the end-to-end Cloud Ops Agent killer demo workflow and print a structured report."""
    config = AgentConfig.from_env()
    if live_aws is not None:
        config.live_aws = live_aws
    orchestrator = CloudOpsOrchestrator(config=config)
    llm_info = orchestrator.gemini.status_summary()

    # 1. Inject bad-release on salescreate-api and run Investigator + Remediator with multimodal context
    sample_attachments = [
        {
            "filename": "salescreate_canary_runbook.md",
            "mime_type": "text/markdown",
            "content": (
                "# SRE Runbook: salescreate-api Canary Regression\n"
                "If HTTP 500 KeyError: 'discount_tier' spikes after canary deploy, diff active task definition "
                "against baseline salescreate-api:14 (v2.4.0) and roll back immediately."
            ),
        },
        {
            "filename": "ecs_cluster_topology.svg",
            "mime_type": "image/svg+xml",
            "content": (
                "<svg><text>ALB -> salescreate-api -> SQS FIFO -> inventory-worker -> notifier</text></svg>"
            ),
        },
    ]

    incident = orchestrator.start_investigation(
        service="salescreate-api",
        scenario="bad-release",
        user_query="Why is salescreate-api throwing HTTP 500 errors?",
        attachments=sample_attachments,
    )
    inv = incident.investigation
    prop = incident.proposal

    # 2. Simulate Human-in-the-Loop Approval and run Independent Verifier
    resolved = orchestrator.approve_remediation(
        incident_id=incident.incident_id,
        approver="sre-oncall@cloudops.io",
    )
    ver = resolved.verification

    # 3. Generate Multimodal Artifacts (.xlsx, .svg, .docx, .md)
    artifacts = orchestrator.generate_artifacts(incident.incident_id)

    # 4. Also demonstrate Environment Memory off-business hours scaling & IAM Guardrail enforcement
    scaling_chat = orchestrator.handle_chat(
        "Scale down the SalesCreate ECS Service for off-business hours"
    )
    guardrail_chat = orchestrator.handle_chat(
        "aws ecs delete-cluster --cluster cloud-ops-demo-cluster"
    )

    summary_payload = {
        "incident_id": resolved.incident_id,
        "service": resolved.service,
        "scenario": resolved.scenario,
        "model": config.gemini_model,
        "llm_status": llm_info,
        "live_aws": config.live_aws,
        "status": resolved.status,
        "investigation_summary": inv.summary if inv else "",
        "root_cause": inv.root_cause if inv else "",
        "hypotheses": [h.to_dict() for h in (inv.hypotheses if inv else [])],
        "matched_past_incident": inv.matched_past_incident if inv else None,
        "remediation_proposal": prop.to_dict() if prop else None,
        "verification": ver.to_dict() if ver else None,
        "artifacts": [a.to_dict() for a in artifacts],
        "off_hours_scaling_proposal": scaling_chat.get("proposal"),
        "destructive_command_blocked": guardrail_chat.get("validation"),
    }

    if as_json:
        print(json.dumps(summary_payload, indent=2))
        return summary_payload

    print("=" * 88)
    print(
        f"CLOUD OPS AGENT — END-TO-END AUTONOMOUS SRE DEMO "
        f"({config.gemini_model} | {llm_info['auth_mode']} | project={llm_info['vertex_project']})"
    )
    print("=" * 88)
    print(f"[1] INCIDENT DETECTED : {incident.incident_id} on service '{incident.service}' (scenario: {incident.scenario})")
    if inv:
        print(f"    Pre-Fix Telemetry : Error Rate = {inv.metrics_snapshot['error_rate_pct']}%, "
              f"P95 Latency = {inv.metrics_snapshot['p95_latency_ms']}ms, RSS = {inv.metrics_snapshot['memory_rss_mb']}MB")
        print("\n[2] INVESTIGATOR 4-STEP DIAGNOSTIC & COURSE ADJUSTMENT:")
        for h in inv.hypotheses:
            print(f"    - [{h.status:<9}] {h.id}: {h.title}")
        if inv.task_def_diff:
            td = inv.task_def_diff
            print(f"    - ECS Task Diff   : {td.baseline_revision} -> {td.active_revision} | {td.changed_env}")
        if inv.matched_past_incident:
            mp = inv.matched_past_incident
            print(f"    - Memory Match    : {mp['incident_id']} ({mp['title']})")
        print(f"    - Root Cause      : {inv.root_cause}")

    if prop:
        print("\n[3] REMEDIATOR PROPOSAL & HUMAN APPROVAL GATE:")
        print(f"    - Proposal ID     : {prop.proposal_id} (Tool: {prop.mcp_tool})")
        print(f"    - Action          : {prop.title}")
        print(f"    - AWS CLI         : {prop.aws_cli_equivalent}")
        print(f"    - Approval Status : {prop.status} (Approved by sre-oncall@cloudops.io)")

    if ver:
        print("\n[4] INDEPENDENT VERIFIER POST-ACTION AUDIT:")
        print(f"    - Verified        : {ver.verified} (Status: {resolved.status})")
        print(f"    - Summary         : {ver.summary}")

    print("\n[5] GENERATED MULTIMODAL WORK ARTIFACTS:")
    for art in artifacts:
        print(f"    - [{art.kind.upper():<8}] {art.name} ({art.size_bytes} bytes) -> {art.path}")

    print("\n[6] ENVIRONMENT MEMORY & IAM GUARDRAILS VERIFIED:")
    sc_prop = scaling_chat.get("proposal") or {}
    print(f"    - Off-Hours Scale : Proposed {sc_prop.get('title')} (Status: {sc_prop.get('status')})")
    val = guardrail_chat.get("validation") or {}
    print(f"    - Guardrail Check : '{val.get('command')}' -> allowed={val.get('allowed')} ({val.get('category')})")
    print("=" * 88)

    return summary_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloud_ops",
        description="Cloud Ops Agent — Multi-Agent AWS ECS & CloudWatch SRE Copilot",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the HTTP API and Modern Web Console server (default command)",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    serve_parser.add_argument(
        "--live-aws",
        action="store_true",
        help="Connect local agent MCP servers to live AWS ECS & CloudWatch",
    )

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run an automated end-to-end CLI walkthrough of the Cloud Ops Agent lifecycle",
    )
    demo_parser.add_argument("--json", action="store_true", help="Emit JSON output")
    demo_parser.add_argument(
        "--live-aws",
        action="store_true",
        help="Run CLI demo against live AWS ECS & CloudWatch",
    )

    args = parser.parse_args(argv)

    if args.command == "demo":
        run_cli_demo(
            as_json=bool(args.json),
            live_aws=True if getattr(args, "live_aws", False) else None,
        )
        return 0

    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8000))
    config = AgentConfig.from_env()
    if getattr(args, "live_aws", False):
        config.live_aws = True
    orchestrator = CloudOpsOrchestrator(config=config)
    run_server(host=host, port=port, orchestrator=orchestrator)
    return 0


if __name__ == "__main__":
    sys.exit(main())
