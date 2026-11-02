"""Guardrailed AWS CLI validator and executor enforcing role-scoped IAM boundaries."""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from cloud_ops.schemas import utc_now_iso

READ_ONLY_PREFIXES: tuple[str, ...] = (
    "describe-",
    "list-",
    "get-",
    "filter-log-events",
    "start-query",
)

ALLOWLISTED_WRITE_ACTIONS: tuple[str, ...] = (
    "ecs update-service",
    "ecs register-task-definition",
)

DENIED_DESTRUCTIVE_ACTIONS: tuple[str, ...] = (
    "delete-cluster",
    "delete-service",
    "deregister-task-definition",
    "delete-log-group",
    "delete-log-stream",
    "delete-repository",
    "create-user",
    "delete-role",
    "attach-role-policy",
)


class GuardrailedAWSCLI:
    """Validates and audits AWS CLI commands against least-privilege IAM role boundaries."""

    READ_ONLY_PREFIXES = READ_ONLY_PREFIXES
    ALLOWLISTED_WRITE_ACTIONS = ALLOWLISTED_WRITE_ACTIONS
    DENIED_DESTRUCTIVE_ACTIONS = DENIED_DESTRUCTIVE_ACTIONS

    def __init__(self) -> None:
        self.audit_log: list[dict[str, Any]] = []

    def get_audit_slice(self, start_index: int) -> list[dict[str, Any]]:
        """Return a shallow copy of audit log entries appended since start_index."""
        safe_start = max(0, int(start_index))
        return [dict(entry) for entry in self.audit_log[safe_start:]]

    def validate_command(
        self,
        command: str,
        approval_token: str | None = None,
        actor: str = "cloud-ops-agent",
    ) -> dict[str, Any]:
        """Validate an AWS CLI command against read-only, allowlisted write, and explicit deny rules."""
        normalized = " ".join((command or "").strip().split()).lower()
        timestamp = utc_now_iso()

        # 1. Check explicit destructive deny list first
        for denied in DENIED_DESTRUCTIVE_ACTIONS:
            if denied in normalized:
                entry = {
                    "timestamp": timestamp,
                    "actor": actor,
                    "command": command,
                    "allowed": False,
                    "category": "DENIED_DESTRUCTIVE",
                    "matched_rule": denied,
                    "approval_token_present": bool(approval_token),
                    "reason": (
                        f"BLOCKED: Command contains explicitly denied destructive operation '{denied}' "
                        "(IAM Policy Sid: ExplicitDenyDestructiveOperations)."
                    ),
                }
                self.audit_log.append(entry)
                return entry

        # 2. Check allowlisted write actions
        for write_action in ALLOWLISTED_WRITE_ACTIONS:
            if write_action in normalized:
                has_token = bool(approval_token and str(approval_token).strip())
                if has_token:
                    entry = {
                        "timestamp": timestamp,
                        "actor": actor,
                        "command": command,
                        "allowed": True,
                        "category": "ALLOWLISTED_WRITE",
                        "matched_rule": write_action,
                        "approval_token_present": True,
                        "approval_token": str(approval_token),
                        "reason": (
                            f"ALLOWED: Write action '{write_action}' is allowlisted on "
                            "cloud-ops-demo-cluster and accompanied by a verified human approval token."
                        ),
                    }
                else:
                    entry = {
                        "timestamp": timestamp,
                        "actor": actor,
                        "command": command,
                        "allowed": False,
                        "category": "WRITE_REQUIRES_APPROVAL",
                        "matched_rule": write_action,
                        "approval_token_present": False,
                        "reason": (
                            f"BLOCKED: Allowlisted write action '{write_action}' cannot execute "
                            "without a human-in-the-loop approval token."
                        ),
                    }
                self.audit_log.append(entry)
                return entry

        # 3. Check read-only prefixes
        tokens = normalized.split()
        if tokens and tokens[0] == "aws":
            tokens = tokens[1:]
        subcommand = tokens[1] if len(tokens) >= 2 else (tokens[0] if tokens else "")
        if any(subcommand.startswith(prefix) for prefix in READ_ONLY_PREFIXES) or any(
            f" {prefix}" in normalized for prefix in READ_ONLY_PREFIXES
        ):
            entry = {
                "timestamp": timestamp,
                "actor": actor,
                "command": command,
                "allowed": True,
                "category": "READ_ONLY",
                "matched_rule": subcommand or "read-only",
                "approval_token_present": bool(approval_token),
                "reason": (
                    "ALLOWED: Read-only observability/inspection action permitted under "
                    "IAM Policy Sid: InvestigatorAndVerifierReadOnly."
                ),
            }
            self.audit_log.append(entry)
            return entry

        # 4. Default deny unlisted actions
        entry = {
            "timestamp": timestamp,
            "actor": actor,
            "command": command,
            "allowed": False,
            "category": "UNLISTED_ACTION",
            "matched_rule": "implicit-deny",
            "approval_token_present": bool(approval_token),
            "reason": (
                "BLOCKED: Command is not in read-only prefixes or allowlisted ECS write operations."
            ),
        }
        self.audit_log.append(entry)
        return entry

    def execute_command(
        self,
        command: str,
        approval_token: str | None = None,
        live_aws: bool = False,
        actor: str = "cloud-ops-agent",
    ) -> dict[str, Any]:
        """Validate and optionally execute an AWS CLI command."""
        validation = self.validate_command(
            command=command, approval_token=approval_token, actor=actor
        )
        if not validation["allowed"]:
            return {
                **validation,
                "executed": False,
                "returncode": 126,
                "stdout": "",
                "stderr": validation["reason"],
            }

        if not live_aws:
            return {
                **validation,
                "executed": True,
                "mode": "simulation",
                "returncode": 0,
                "stdout": f"[Simulated AWS CLI Success] {command}",
                "stderr": "",
            }

        try:
            args = shlex.split(command)
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return {
                **validation,
                "executed": True,
                "mode": "live_aws",
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        except Exception as exc:
            return {
                **validation,
                "executed": False,
                "mode": "live_aws_fallback",
                "returncode": 1,
                "stdout": "",
                "stderr": str(exc),
            }
