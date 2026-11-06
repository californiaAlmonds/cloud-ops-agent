"""Guardrailed AWS CLI validator and executor enforcing role-scoped IAM boundaries.

The guard parses a command into (service, operation, options) *before* deciding anything, and
the exact argument vector that was validated is the one that gets executed. Nothing is matched
by substring, nothing runs unless argv[0] is the AWS CLI, and writes need a single-use approval
token issued by the ApprovalLedger for a human-approved proposal. IAM remains the authoritative
boundary; this layer exists so unsafe commands are refused before they reach AWS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
import subprocess
from typing import Any

from cloud_ops.approvals import ApprovalLedger
from cloud_ops.schemas import utc_now_iso

# Operations in these services may be used read-only when they start with describe-/list-/get-.
READ_ONLY_SERVICES: frozenset[str] = frozenset(
    {
        "ecs", "logs", "cloudwatch", "ec2", "rds", "sqs", "sns", "elbv2", "autoscaling",
        "application-autoscaling", "cloudtrail", "ce", "events", "ecr", "s3api", "route53",
        "cloudformation",
    }
)
READ_ONLY_PREFIXES: tuple[str, ...] = ("describe-", "list-", "get-")
EXTRA_READ_ONLY_OPERATIONS: dict[str, frozenset[str]] = {
    "logs": frozenset({"filter-log-events", "start-query"}),
}
# Reads that return credentials, secrets, user data or object contents. Always refused.
SENSITIVE_READ_OPERATIONS: frozenset[str] = frozenset(
    {
        "get-secret-value", "get-password-data", "get-object", "get-object-torrent",
        "get-session-token", "get-federation-token", "get-authorization-token",
        "get-login-password", "get-parameter", "get-parameters", "get-parameters-by-path",
        "get-credential-report", "get-account-authorization-details", "get-access-key-last-used",
        "describe-instance-attribute", "get-console-output", "get-function",
        "get-function-configuration",
    }
)
# IAM is limited to exactly what infra/agent_iam_policy.json grants.
IAM_READ_OPERATIONS: frozenset[str] = frozenset(
    {"get-role", "list-attached-role-policies", "list-role-policies", "get-policy", "get-policy-version"}
)

ALLOWLISTED_WRITE_ACTIONS: tuple[str, ...] = (
    "ecs update-service",
    "ecs register-task-definition",
)
_WRITE_ALLOWED_FLAGS: dict[str, frozenset[str]] = {
    "ecs update-service": frozenset(
        {
            "--cluster", "--service", "--task-definition", "--desired-count",
            "--force-new-deployment", "--no-force-new-deployment", "--region", "--output",
            "--no-cli-pager",
        }
    ),
    "ecs register-task-definition": frozenset({"--family", "--region", "--output", "--no-cli-pager"}),
}

_BOOLEAN_FLAGS: frozenset[str] = frozenset(
    {"--force-new-deployment", "--no-force-new-deployment", "--no-cli-pager"}
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
_DESTRUCTIVE_PREFIXES: tuple[str, ...] = (
    "delete-", "terminate-", "deregister-", "destroy-", "purge-", "remove-", "detach-", "revoke-",
)

# Options that redirect traffic, swap credentials, load local files or disable TLS.
_BLOCKED_FLAGS: frozenset[str] = frozenset(
    {
        "--endpoint-url", "--profile", "--ca-bundle", "--no-verify-ssl", "--no-sign-request",
        "--cli-input-json", "--cli-input-yaml", "--generate-cli-skeleton", "--debug",
        "--cli-auto-prompt",
    }
)
_CHAIN_TOKENS: frozenset[str] = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<"})
_PARAM_FILE_PREFIXES: tuple[str, ...] = ("file://", "fileb://", "http://", "https://")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FLAG_RE = re.compile(r"^--[a-z0-9][a-z0-9-]*$")
_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+(\.\d+)?$")
_TASKDEF_RE = re.compile(r"^(?:arn:aws:ecs:[a-z0-9-]+:\d{12}:task-definition/)?([A-Za-z0-9_-]+)(?::(\d+))?$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")


@dataclass
class ParsedCommand:
    """A command split into the pieces the guard reasons about (and later executes)."""

    argv: list[str]
    service: str
    operation: str
    flags: dict[str, list[str]] = field(default_factory=dict)
    stray: list[str] = field(default_factory=list)


class GuardrailedAWSCLI:
    """Validates and audits AWS CLI commands against least-privilege IAM role boundaries."""

    READ_ONLY_PREFIXES = READ_ONLY_PREFIXES
    ALLOWLISTED_WRITE_ACTIONS = ALLOWLISTED_WRITE_ACTIONS
    DENIED_DESTRUCTIVE_ACTIONS = DENIED_DESTRUCTIVE_ACTIONS

    def __init__(
        self,
        allowed_cluster: str = "cloud-ops-demo-cluster",
        allowed_services: tuple[str, ...] | list[str] | None = None,
        approval_ledger: ApprovalLedger | None = None,
    ) -> None:
        self.audit_log: list[dict[str, Any]] = []
        self.allowed_cluster = allowed_cluster
        self.allowed_services = frozenset(allowed_services) if allowed_services else None
        self.approval_ledger = approval_ledger

    def get_audit_slice(self, start_index: int) -> list[dict[str, Any]]:
        """Return a shallow copy of audit log entries appended since start_index."""
        safe_start = max(0, int(start_index))
        return [dict(entry) for entry in self.audit_log[safe_start:]]

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def parse_command(command: str) -> tuple[ParsedCommand | None, str]:
        """Split a command into service, operation and options. Returns (None, reason) if unsafe."""
        if not isinstance(command, str) or not command.strip():
            return None, "empty command"
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return None, f"unparseable command ({exc})"
        if len(argv) < 3 or argv[0] != "aws":
            return None, "command must have the form: aws <service> <operation> [options]"
        service, operation = argv[1], argv[2]
        if not _NAME_RE.match(service) or not _NAME_RE.match(operation):
            return None, "invalid service or operation name"

        flags: dict[str, list[str]] = {}
        stray: list[str] = []
        current: str | None = None
        for token in argv[3:]:
            if token in _CHAIN_TOKENS:
                return None, "command chaining or redirection is not permitted"
            if token.lower().startswith("--"):
                name, has_eq, value = token.partition("=")
                name = name.lower()
                if not _FLAG_RE.match(name):
                    return None, f"invalid option '{token}'"
                flags.setdefault(name, [])
                current = name
                if not has_eq:
                    continue
                token = value
            elif token.startswith("-") and len(token) > 1 and not _NEGATIVE_NUMBER_RE.match(token):
                return None, f"short options are not permitted ('{token}')"
            if token.lower().startswith(_PARAM_FILE_PREFIXES):
                return None, "file:// and URL parameter values are not permitted"
            (flags[current] if current else stray).append(token)
        return ParsedCommand(argv=argv, service=service, operation=operation, flags=flags, stray=stray), ""

    @staticmethod
    def _blocked_flag(parsed: ParsedCommand) -> str | None:
        for name in parsed.flags:
            # Also catch unambiguous abbreviations such as --endpoint for --endpoint-url.
            if name in _BLOCKED_FLAGS or any(b.startswith(name) and len(name) > 2 for b in _BLOCKED_FLAGS):
                return name
        return None

    @staticmethod
    def _is_read_only(service: str, operation: str) -> bool:
        if service == "iam":
            return operation in IAM_READ_OPERATIONS
        if service == "sts":
            return operation == "get-caller-identity"
        if service not in READ_ONLY_SERVICES or operation in SENSITIVE_READ_OPERATIONS:
            return False
        if operation.startswith(READ_ONLY_PREFIXES):
            return True
        return operation in EXTRA_READ_ONLY_OPERATIONS.get(service, frozenset())

    # --------------------------------------------------------------- write scope

    def _write_scope_error(self, key: str, parsed: ParsedCommand) -> str | None:
        """Return why a write command is out of scope, or None if it is within bounds."""
        allowed_flags = _WRITE_ALLOWED_FLAGS[key]
        for name in parsed.flags:
            if name not in allowed_flags:
                return f"option '{name}' is not permitted for '{key}'"
        if parsed.stray:
            return "unexpected positional arguments"
        for name, values in parsed.flags.items():
            if name in _BOOLEAN_FLAGS:
                if values:
                    return f"option '{name}' does not take a value"
            elif len(values) != 1:
                return f"option '{name}' must be given exactly once with one value"

        def one(flag: str) -> str | None:
            vals = parsed.flags.get(flag) or []
            return vals[0] if len(vals) == 1 else None

        region = one("--region")
        if region is not None and not _REGION_RE.match(region):
            return f"invalid region '{region}'"

        if key == "ecs update-service":
            cluster, service = one("--cluster"), one("--service")
            if cluster != self.allowed_cluster:
                return f"cluster must be '{self.allowed_cluster}'"
            if not service or (self.allowed_services and service not in self.allowed_services):
                return "service is not one of the managed demo services"
            taskdef = one("--task-definition")
            if taskdef is not None:
                match = _TASKDEF_RE.match(taskdef)
                if not match or match.group(1) != service:
                    return "task definition must belong to the same service"
            count = one("--desired-count")
            if count is not None and not (count.isdigit() and 0 <= int(count) <= 10):
                return "desired count must be an integer between 0 and 10"
        else:
            family = one("--family")
            if not family or (self.allowed_services and family not in self.allowed_services):
                return "task definition family is not one of the managed demo services"
        return None

    # -------------------------------------------------------------- validation

    def _entry(
        self,
        command: str,
        actor: str,
        allowed: bool,
        category: str,
        matched_rule: str,
        reason: str,
        token: str | None,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": utc_now_iso(),
            "actor": actor,
            "command": command,
            "allowed": allowed,
            "category": category,
            "matched_rule": matched_rule,
            "approval_token_present": bool(token and str(token).strip()),
            "reason": reason,
        }
        self.audit_log.append(entry)
        return entry

    def validate_command(
        self,
        command: str,
        approval_token: str | None = None,
        actor: str = "cloud-ops-agent",
        consume: bool = False,
    ) -> dict[str, Any]:
        """Validate a command against read-only, allowlisted-write and explicit deny rules.

        With consume=True a valid approval token is spent (single use). execute_command does this.
        """
        def deny(category: str, rule: str, reason: str) -> dict[str, Any]:
            return self._entry(command, actor, False, category, rule, reason, approval_token)

        parsed, error = self.parse_command(command)
        if parsed is None:
            return deny("UNLISTED_ACTION", "malformed-command", f"BLOCKED: {error}.")
        blocked = self._blocked_flag(parsed)
        if blocked:
            return deny("UNLISTED_ACTION", blocked, f"BLOCKED: option '{blocked}' is not permitted.")

        service, operation = parsed.service, parsed.operation

        if operation in DENIED_DESTRUCTIVE_ACTIONS or operation.startswith(_DESTRUCTIVE_PREFIXES):
            return deny(
                "DENIED_DESTRUCTIVE",
                operation,
                f"BLOCKED: Command contains explicitly denied destructive operation '{operation}' "
                "(IAM Policy Sid: ExplicitDenyDestructiveOperations).",
            )

        write_key = f"{service} {operation}"
        if write_key in ALLOWLISTED_WRITE_ACTIONS:
            token = str(approval_token).strip() if approval_token else ""
            if not token:
                return deny(
                    "WRITE_REQUIRES_APPROVAL",
                    write_key,
                    f"BLOCKED: Allowlisted write action '{write_key}' cannot execute "
                    "without a human-in-the-loop approval token.",
                )
            if self.approval_ledger is None or not self.approval_ledger.is_valid(token):
                return deny(
                    "WRITE_REQUIRES_APPROVAL",
                    write_key,
                    f"BLOCKED: The approval token for '{write_key}' is unknown, expired or already used.",
                )
            scope_error = self._write_scope_error(write_key, parsed)
            if scope_error:
                return deny("WRITE_OUT_OF_SCOPE", write_key, f"BLOCKED: {scope_error}.")
            if consume and not self.approval_ledger.consume(token):
                return deny(
                    "WRITE_REQUIRES_APPROVAL",
                    write_key,
                    f"BLOCKED: The approval token for '{write_key}' was already used.",
                )
            return self._entry(
                command,
                actor,
                True,
                "ALLOWLISTED_WRITE",
                write_key,
                f"ALLOWED: Write action '{write_key}' is allowlisted on {self.allowed_cluster} "
                "and accompanied by a verified, single-use human approval token.",
                approval_token,
            )

        if self._is_read_only(service, operation):
            return self._entry(
                command,
                actor,
                True,
                "READ_ONLY",
                operation,
                "ALLOWED: Read-only observability/inspection action permitted under "
                "IAM Policy Sid: InvestigatorAndVerifierReadOnly.",
                approval_token,
            )

        return deny(
            "UNLISTED_ACTION",
            "implicit-deny",
            "BLOCKED: Command is not in read-only prefixes or allowlisted ECS write operations.",
        )

    def execute_command(
        self,
        command: str,
        approval_token: str | None = None,
        live_aws: bool = False,
        actor: str = "cloud-ops-agent",
    ) -> dict[str, Any]:
        """Validate and optionally execute an AWS CLI command."""
        validation = self.validate_command(
            command=command, approval_token=approval_token, actor=actor, consume=True
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

        parsed, error = self.parse_command(command)
        if parsed is None:  # cannot happen after a successful validation, but fail closed
            return {**validation, "executed": False, "returncode": 126, "stdout": "", "stderr": error}
        try:
            proc = subprocess.run(
                parsed.argv,  # exactly the argv that was validated, never a shell
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
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
