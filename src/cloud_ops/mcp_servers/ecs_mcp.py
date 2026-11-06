"""ECS MCP Server with dynamic service discovery, task definition diffing, and approval-gated writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from cloud_ops.config import ROOT_DIR
from cloud_ops.mcp_servers.aws_cli_guard import GuardrailedAWSCLI
from cloud_ops.mcp_servers.cloudwatch_mcp import SUPPORTED_SERVICES, CloudWatchMCPServer
from cloud_ops.schemas import TaskDefDiff, utc_now_iso

BASELINE_REVISIONS: dict[str, int] = {
    "salescreate-api": 14,
    "inventory-worker": 9,
    "notifier": 11,
}
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RemediationExecutionError(RuntimeError):
    """An approved remediation could not be carried out against the target environment."""


def baseline_revision_number(service: str) -> int:
    """Known-good revision number; CLOUD_OPS_BASELINE_REVISIONS="svc=3,other=5" overrides defaults."""
    for part in os.environ.get("CLOUD_OPS_BASELINE_REVISIONS", "").split(","):
        name, _, num = part.strip().partition("=")
        if name.strip() == service and num.strip().isdigit():
            return int(num.strip())
    return BASELINE_REVISIONS.get(service, 10)


class ECSMCPServer:
    """MCP Server for Amazon ECS service inspection, task definition diffing, and guardrailed writes."""

    def __init__(
        self,
        cloudwatch_mcp: CloudWatchMCPServer,
        cli_guard: GuardrailedAWSCLI | None = None,
        cluster: str = "cloud-ops-demo-cluster",
        region: str = "us-east-1",
        iam_policy_path: Path | None = None,
        live_aws: bool = False,
    ) -> None:
        self.cloudwatch = cloudwatch_mcp
        self.cli_guard = cli_guard or cloudwatch_mcp.cli_guard
        self.cluster = cluster
        self.region = region
        self.iam_policy_path = (
            Path(iam_policy_path)
            if iam_policy_path
            else (ROOT_DIR / "infra" / "agent_iam_policy.json")
        )
        self.live_aws = live_aws

        self.desired_counts: dict[str, int] = {
            "salescreate-api": 2,
            "inventory-worker": 2,
            "notifier": 2,
        }
        self.revision_counters: dict[str, int] = {
            "salescreate-api": 15,
            "inventory-worker": 9,
            "notifier": 11,
        }

    def get_active_revision(self, service: str) -> str:
        """Return the active task definition revision string (`<service>:<rev>`) for a service."""
        svc = self._validate_service(service)
        base_num = baseline_revision_number(svc)
        scenario = self.cloudwatch.get_service_scenario(svc)
        if scenario == "healthy":
            return f"{svc}:{base_num}"
        active_num = max(base_num + 1, self.revision_counters.get(svc, base_num + 1))
        self.revision_counters[svc] = active_num
        return f"{svc}:{active_num}"

    def get_baseline_revision(self, service: str) -> str:
        """Return the known-good healthy baseline task definition revision (`<service>:<rev>`)."""
        svc = self._validate_service(service)
        base_num = baseline_revision_number(svc)
        return f"{svc}:{base_num}"

    # -------------------------------------------------------------------------
    # Read-Only Tools (Investigator & Verifier)
    # -------------------------------------------------------------------------

    def list_ecs_services(self) -> dict[str, Any]:
        """List all ECS services in the cluster with live or simulated status."""
        cmd = f"aws ecs list-services --cluster {self.cluster} --region {self.region}"
        self.cli_guard.execute_command(
            cmd,
            live_aws=self.live_aws,
            actor="investigator-agent",
        )

        discovered_names: list[str] = []
        if self.live_aws:
            discovered_names = self._try_list_live_ecs_services()

        if not discovered_names:
            discovered_names = list(self.cloudwatch.active_scenarios.keys())

        services_summary: list[dict[str, Any]] = []
        for svc_name in discovered_names:
            try:
                services_summary.append(self.describe_ecs_service(svc_name))
            except Exception:
                continue

        return {
            "cluster": self.cluster,
            "region": self.region,
            "live_aws": self.live_aws,
            "service_count": len(services_summary),
            "services": services_summary,
        }

    def _try_list_live_ecs_services(self) -> list[str]:
        try:
            proc = subprocess.run(
                [
                    "aws",
                    "ecs",
                    "list-services",
                    "--cluster",
                    self.cluster,
                    "--region",
                    self.region,
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return []
            payload = json.loads(proc.stdout)
            arns = payload.get("serviceArns") or []
            names: list[str] = []
            for arn in arns:
                name = str(arn).split("/")[-1].strip()
                if name:
                    self._validate_service(name)
                    names.append(name)
            return names
        except Exception:
            return []

    def describe_ecs_service(self, service: str) -> dict[str, Any]:
        """Inspect ECS service state, desired/running task counts, and active task definition."""
        svc = self._validate_service(service)
        cmd = (
            f"aws ecs describe-services --cluster {self.cluster} "
            f"--services {svc} --region {self.region}"
        )
        self.cli_guard.validate_command(cmd, actor="investigator-agent")

        scenario = self.cloudwatch.get_service_scenario(svc)
        env = self.cloudwatch.get_resolved_env(svc, scenario)
        desired = self.desired_counts.get(svc, 2)
        running = desired
        pending = 0
        active_rev = self.get_active_revision(svc)
        base_rev = self.get_baseline_revision(svc)
        log_group = f"/ecs/cloud-ops-demo/{svc}"
        rollout_state = "COMPLETED"

        if self.live_aws:
            live_svc = self._try_describe_live_ecs_service(svc)
            if not live_svc:
                rollout_state = "UNKNOWN"
            if live_svc:
                primary = next(
                    (d for d in (live_svc.get("deployments") or []) if d.get("status") == "PRIMARY"),
                    None,
                )
                rollout_state = str((primary or {}).get("rolloutState") or "UNKNOWN")
                desired = int(live_svc.get("desiredCount", desired))
                running = int(live_svc.get("runningCount", running))
                pending = int(live_svc.get("pendingCount", 0))
                td_arn = str(live_svc.get("taskDefinition", ""))
                if "/" in td_arn:
                    active_rev = td_arn.split("/")[-1]
                td_info = self._try_describe_live_task_definition(active_rev)
                if td_info:
                    containers = td_info.get("containerDefinitions") or []
                    if containers:
                        c0 = containers[0]
                        log_opts = (c0.get("logConfiguration") or {}).get("options") or {}
                        if log_opts.get("awslogs-group"):
                            log_group = str(log_opts["awslogs-group"])
                        live_env_list = c0.get("environment") or []
                        if live_env_list:
                            env = {
                                str(item.get("name")): str(item.get("value", ""))
                                for item in live_env_list
                                if item.get("name")
                            }

        health_status = "HEALTHY" if scenario == "healthy" else "DEGRADED"
        if self.live_aws and (rollout_state in ("IN_PROGRESS", "FAILED", "UNKNOWN") or running != desired):
            health_status = "DEGRADED"

        return {
            "cluster": self.cluster,
            "service": svc,
            "status": "ACTIVE",
            "scenario": scenario,
            "health_status": health_status,
            "rollout_state": rollout_state,
            "desired_count": desired,
            "running_count": running,
            "pending_count": pending,
            "launch_type": "FARGATE",
            "active_task_definition": active_rev,
            "baseline_task_definition": base_rev,
            "release_version": env.get("RELEASE_VERSION", "v1.0.0"),
            "environment": env,
            "log_group": log_group,
            "live_aws": self.live_aws,
            "last_deployment_at": utc_now_iso(),
        }

    def _try_describe_live_ecs_service(self, service: str) -> dict[str, Any] | None:
        """Query remote AWS ECS service state when live_aws=True."""
        try:
            proc = subprocess.run(
                [
                    "aws",
                    "ecs",
                    "describe-services",
                    "--cluster",
                    self.cluster,
                    "--services",
                    service,
                    "--region",
                    self.region,
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            payload = json.loads(proc.stdout)
            services = payload.get("services") or []
            return services[0] if services else None
        except Exception:
            return None

    def _try_describe_live_task_definition(self, task_definition: str) -> dict[str, Any] | None:
        try:
            proc = subprocess.run(
                [
                    "aws",
                    "ecs",
                    "describe-task-definition",
                    "--task-definition",
                    task_definition,
                    "--region",
                    self.region,
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            payload = json.loads(proc.stdout)
            return payload.get("taskDefinition")
        except Exception:
            return None

    def list_task_definition_revisions(self, service: str) -> list[dict[str, Any]]:
        """List task definition revisions for a service family."""
        svc = self._validate_service(service)
        cmd = (
            f"aws ecs list-task-definitions --family-prefix {svc} "
            f"--sort DESC --region {self.region}"
        )
        self.cli_guard.validate_command(cmd, actor="investigator-agent")

        base_rev = self.get_baseline_revision(svc)
        active_rev = self.get_active_revision(svc)
        base_env = self.cloudwatch.get_baseline_env(svc)
        active_env = self.cloudwatch.get_resolved_env(svc)

        revisions = [
            {
                "revision": base_rev,
                "status": "ACTIVE",
                "is_baseline": True,
                "is_current": active_rev == base_rev,
                "release_version": base_env.get("RELEASE_VERSION", ""),
                "environment": base_env,
            }
        ]
        if active_rev != base_rev:
            revisions.insert(
                0,
                {
                    "revision": active_rev,
                    "status": "ACTIVE",
                    "is_baseline": False,
                    "is_current": True,
                    "release_version": active_env.get("RELEASE_VERSION", ""),
                    "environment": active_env,
                },
            )
        return revisions

    def diff_task_definitions(
        self,
        service: str,
        baseline_revision: int | str | None = None,
        active_revision: int | str | None = None,
    ) -> TaskDefDiff:
        """Compare the active ECS task definition revision against the healthy baseline revision."""
        svc = self._validate_service(service)
        active_rev = str(active_revision) if active_revision else self.get_active_revision(svc)
        base_rev = str(baseline_revision) if baseline_revision else self.get_baseline_revision(svc)

        cmd = (
            f"aws ecs describe-task-definition --task-definition {active_rev} "
            f"--region {self.region}"
        )
        self.cli_guard.validate_command(cmd, actor="investigator-agent")

        baseline_env = self.cloudwatch.get_baseline_env(svc)
        active_env = self.cloudwatch.get_resolved_env(svc)

        if self.live_aws:
            live_revs = self._try_list_live_task_definitions(svc)
            if len(live_revs) >= 2:
                active_rev = live_revs[0]
                base_rev = live_revs[1]
                active_td = self._try_describe_live_task_definition(active_rev)
                base_td = self._try_describe_live_task_definition(base_rev)
                if active_td and base_td:
                    active_env = self._extract_td_env(active_td)
                    baseline_env = self._extract_td_env(base_td)

        changed_env: dict[str, dict[str, str]] = {}
        all_keys = sorted(set(baseline_env.keys()) | set(active_env.keys()))
        for key in all_keys:
            b_val = str(baseline_env.get(key, ""))
            a_val = str(active_env.get(key, ""))
            if b_val != a_val:
                changed_env[key] = {
                    "baseline": b_val,
                    "active": a_val,
                    "from": b_val,
                    "to": a_val,
                    "summary": f"{b_val} -> {a_val}",
                }

        base_release = baseline_env.get("RELEASE_VERSION", "v1.0.0")
        active_release = active_env.get("RELEASE_VERSION", base_release)
        release_change = {
            "baseline": base_release,
            "active": active_release,
            "from": base_release,
            "to": active_release,
            "changed": "true" if base_release != active_release else "false",
            "summary": f"{base_release} -> {active_release}",
        }

        return TaskDefDiff(
            service=svc,
            active_revision=active_rev,
            baseline_revision=base_rev,
            changed_env=changed_env,
            release_change=release_change,
        )

    def _try_list_live_task_definitions(self, family_prefix: str) -> list[str]:
        try:
            proc = subprocess.run(
                [
                    "aws",
                    "ecs",
                    "list-task-definitions",
                    "--family-prefix",
                    family_prefix,
                    "--sort",
                    "DESC",
                    "--max-items",
                    "5",
                    "--region",
                    self.region,
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return []
            payload = json.loads(proc.stdout)
            arns = payload.get("taskDefinitionArns") or []
            return [str(a).split("/")[-1] for a in arns if a]
        except Exception:
            return []

    @staticmethod
    def _extract_td_env(td: dict[str, Any]) -> dict[str, str]:
        containers = td.get("containerDefinitions") or []
        if not containers:
            return {}
        c0 = containers[0]
        env_map = {
            str(item.get("name")): str(item.get("value", ""))
            for item in (c0.get("environment") or [])
            if item.get("name")
        }
        if c0.get("image"):
            env_map.setdefault("CONTAINER_IMAGE", str(c0["image"]))
        return env_map

    def inspect_iam_role_permissions(self) -> dict[str, Any]:
        """Read infra/agent_iam_policy.json and summarize allowed read-only, write, and denied scopes."""
        cmd = "aws iam list-attached-role-policies --role-name cloud-ops-agent-role"
        self.cli_guard.validate_command(cmd, actor="investigator-agent")

        raw_policy: dict[str, Any] = {"Version": "2012-10-17", "Statement": []}
        if self.iam_policy_path.exists():
            raw_policy = json.loads(self.iam_policy_path.read_text(encoding="utf-8"))

        read_only_actions: list[str] = []
        allowlisted_write_actions: list[str] = []
        allowlisted_write_resources: list[str] = []
        explicit_deny_actions: list[str] = []

        for stmt in raw_policy.get("Statement", []):
            sid = stmt.get("Sid", "")
            effect = stmt.get("Effect", "")
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            resources = stmt.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]

            if effect == "Allow" and "ReadOnly" in sid:
                read_only_actions.extend(actions)
            elif effect == "Allow":
                allowlisted_write_actions.extend(actions)
                for res in resources:
                    if res != "*" and res not in allowlisted_write_resources:
                        allowlisted_write_resources.append(res)
            elif effect == "Deny":
                explicit_deny_actions.extend(actions)

        summary = (
            f"IAM Role Policy ({self.iam_policy_path.name}): Grants {len(read_only_actions)} read-only "
            f"observability actions across ECS/CloudWatch/IAM, restricts write actions strictly to "
            f"{', '.join(allowlisted_write_actions)} on cluster '{self.cluster}' (requiring human approval), "
            f"and enforces an explicit Deny on {len(explicit_deny_actions)} destructive actions "
            f"({', '.join(explicit_deny_actions[:5])}, etc.)."
        )
        return {
            "policy_path": str(self.iam_policy_path),
            "version": raw_policy.get("Version", "2012-10-17"),
            "allowed_read_only_actions": read_only_actions,
            "allowlisted_write_actions": allowlisted_write_actions,
            "allowlisted_write_resources": allowlisted_write_resources,
            "explicitly_denied_actions": explicit_deny_actions,
            "summary": summary,
        }

    # -------------------------------------------------------------------------
    # Guardrailed Write Tools (Require valid approval_token)
    # -------------------------------------------------------------------------

    def rollback_task_definition(
        self,
        service: str,
        target_revision: str,
        approval_token: str,
    ) -> dict[str, Any]:
        """Roll back an ECS service to a known-good task definition revision (approval required)."""
        svc = self._validate_service(service)
        self._require_approval_token(approval_token, "rollback_task_definition")

        if self.live_aws and self._try_describe_live_task_definition(target_revision) is None:
            raise RemediationExecutionError(
                f"Rollback target '{target_revision}' was not found in AWS; refusing to proceed. "
                "Set CLOUD_OPS_BASELINE_REVISIONS to the real known-good revision."
            )

        previous_revision = self.get_active_revision(svc)
        aws_cli_cmd = (
            f"aws ecs update-service --cluster {self.cluster} "
            f"--service {svc} --task-definition {target_revision} "
            f"--force-new-deployment --region {self.region}"
        )
        cli_res = self.cli_guard.execute_command(
            aws_cli_cmd,
            approval_token=approval_token,
            live_aws=self.live_aws,
            actor="remediator-agent",
        )
        if not cli_res["allowed"]:
            raise PermissionError(cli_res["reason"])
        self._require_success(cli_res, "rollback_task_definition")

        # Reset simulated scenario back to healthy so Verifier observes real healthy telemetry
        self.cloudwatch.set_service_scenario(svc, "healthy")

        return {
            "status": "ROLLED_BACK_TO_HEALTHY",
            "service": svc,
            "cluster": self.cluster,
            "previous_revision": previous_revision,
            "target_revision": target_revision,
            "active_revision": self.get_active_revision(svc),
            "scenario": self.cloudwatch.get_service_scenario(svc),
            "approval_token": approval_token,
            "aws_cli_command": aws_cli_cmd,
            "timestamp": utc_now_iso(),
        }

    def update_service_env(
        self,
        service: str,
        env_overrides: dict[str, str],
        approval_token: str,
    ) -> dict[str, Any]:
        """Register a corrected task definition revision with env_overrides and redeploy service."""
        svc = self._validate_service(service)
        self._require_approval_token(approval_token, "update_service_env")

        if self.live_aws:
            raise RemediationExecutionError(
                "Updating environment variables on real AWS is not supported yet; "
                "roll back to the known-good task definition revision instead."
            )

        previous_revision = self.get_active_revision(svc)
        register_cmd = f"aws ecs register-task-definition --family {svc} --region {self.region}"
        update_cmd = (
            f"aws ecs update-service --cluster {self.cluster} --service {svc} "
            f"--force-new-deployment --region {self.region}"
        )
        for step_cmd in (register_cmd, update_cmd):
            cli_res = self.cli_guard.execute_command(
                step_cmd,
                approval_token=approval_token,
                live_aws=False,
                actor="remediator-agent",
            )
            if not cli_res["allowed"]:
                raise PermissionError(cli_res["reason"])
            self._require_success(cli_res, "update_service_env")
        aws_cli_cmd = f"{register_cmd} && {update_cmd}"

        # Reset service back to healthy baseline with corrected env overrides
        clean_overrides = dict(env_overrides)
        clean_overrides["FAULT_MODE"] = "healthy"
        self.cloudwatch.set_service_scenario(svc, "healthy", env_overrides=clean_overrides)

        return {
            "status": "UPDATED_AND_HEALTHY",
            "service": svc,
            "cluster": self.cluster,
            "previous_revision": previous_revision,
            "active_revision": self.get_active_revision(svc),
            "applied_env_overrides": clean_overrides,
            "scenario": self.cloudwatch.get_service_scenario(svc),
            "approval_token": approval_token,
            "aws_cli_command": aws_cli_cmd,
            "timestamp": utc_now_iso(),
        }

    def scale_ecs_service(
        self,
        service: str,
        desired_count: int,
        reason: str,
        approval_token: str,
    ) -> dict[str, Any]:
        """Scale an ECS service desired task count (approval required)."""
        svc = self._validate_service(service)
        self._require_approval_token(approval_token, "scale_ecs_service")
        if desired_count < 0 or desired_count > 10:
            raise ValueError(f"desired_count={desired_count} outside safe operating bounds [0..10]")

        previous_count = self.desired_counts.get(svc, 2)
        aws_cli_cmd = (
            f"aws ecs update-service --cluster {self.cluster} "
            f"--service {svc} --desired-count {desired_count} --region {self.region}"
        )
        cli_res = self.cli_guard.execute_command(
            aws_cli_cmd,
            approval_token=approval_token,
            live_aws=self.live_aws,
            actor="remediator-agent",
        )
        if not cli_res["allowed"]:
            raise PermissionError(cli_res["reason"])
        self._require_success(cli_res, "scale_ecs_service")

        self.desired_counts[svc] = int(desired_count)
        return {
            "status": "SCALED",
            "service": svc,
            "cluster": self.cluster,
            "previous_desired_count": previous_count,
            "desired_count": int(desired_count),
            "reason": reason,
            "approval_token": approval_token,
            "aws_cli_command": aws_cli_cmd,
            "timestamp": utc_now_iso(),
        }

    @staticmethod
    def _require_success(cli_res: dict[str, Any], operation: str) -> None:
        """Fail loudly when the AWS call did not actually succeed instead of reporting a fix."""
        if not cli_res.get("executed") or int(cli_res.get("returncode", 1)) != 0:
            detail = str(cli_res.get("stderr") or cli_res.get("reason") or "no error output")[:300]
            raise RemediationExecutionError(f"'{operation}' did not complete: {detail}")

    def _require_approval_token(self, approval_token: str | None, operation: str) -> None:
        if not approval_token or not str(approval_token).strip():
            self.cli_guard.validate_command(
                f"aws ecs update-service ({operation})",
                approval_token=None,
                actor="remediator-agent",
            )
            raise PermissionError(
                f"Guardrail violation: '{operation}' requires a valid human approval_token."
            )

    def _validate_service(self, service: str) -> str:
        svc = self.cloudwatch._validate_service(service)
        self.desired_counts.setdefault(svc, 2)
        self.revision_counters.setdefault(svc, BASELINE_REVISIONS.get(svc, 10) + 1)
        return svc
