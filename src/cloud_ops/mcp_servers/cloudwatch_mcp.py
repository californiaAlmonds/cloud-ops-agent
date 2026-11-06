"""CloudWatch Logs & Metrics MCP Server backed by the 3 real microservices and optional live AWS CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from cloud_ops.config import ROOT_DIR
from cloud_ops.mcp_servers.aws_cli_guard import GuardrailedAWSCLI

SUPPORTED_SERVICES: tuple[str, ...] = ("salescreate-api", "inventory-worker", "notifier")
DEFAULT_SIMULATED_SERVICES: tuple[str, ...] = SUPPORTED_SERVICES
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _ensure_service_imports(services_dir: Path) -> dict[str, dict[str, Any]]:
    """Dynamically add each microservice's src directory to sys.path and import its runtime modules."""
    for svc in SUPPORTED_SERVICES:
        src_path = str((services_dir / svc / "src").resolve())
        if src_path not in sys.path:
            sys.path.insert(0, src_path)

    from inventory_worker.app import generate_tick_event as inv_tick
    from inventory_worker.scenarios import (
        BASELINE_ENV as INV_BASELINE_ENV,
        SCENARIOS as INV_SCENARIOS,
    )
    from notifier.app import generate_tick_event as ntf_tick
    from notifier.scenarios import (
        BASELINE_ENV as NTF_BASELINE_ENV,
        SCENARIOS as NTF_SCENARIOS,
    )
    from salescreate_api.app import generate_tick_event as sales_tick
    from salescreate_api.scenarios import (
        BASELINE_ENV as SALES_BASELINE_ENV,
        SCENARIOS as SALES_SCENARIOS,
    )

    return {
        "salescreate-api": {
            "generate_tick_event": sales_tick,
            "SCENARIOS": SALES_SCENARIOS,
            "BASELINE_ENV": SALES_BASELINE_ENV,
        },
        "inventory-worker": {
            "generate_tick_event": inv_tick,
            "SCENARIOS": INV_SCENARIOS,
            "BASELINE_ENV": INV_BASELINE_ENV,
        },
        "notifier": {
            "generate_tick_event": ntf_tick,
            "SCENARIOS": NTF_SCENARIOS,
            "BASELINE_ENV": NTF_BASELINE_ENV,
        },
    }


class CloudWatchMCPServer:
    """MCP server exposing CloudWatch log querying and metric aggregation across ECS services."""

    def __init__(
        self,
        services_dir: Path | None = None,
        live_aws: bool = False,
        region: str = "us-east-1",
        cli_guard: GuardrailedAWSCLI | None = None,
        cluster: str = "cloud-ops-demo-cluster",
    ) -> None:
        self.services_dir = Path(services_dir) if services_dir else (ROOT_DIR / "services")
        self.live_aws = live_aws
        self.region = region
        self.cluster = cluster
        self.cli_guard = cli_guard or GuardrailedAWSCLI()
        self._modules = _ensure_service_imports(self.services_dir)

        # Default active scenarios: salescreate-api starts in 'bad-release' for instant demo readiness
        self.active_scenarios: dict[str, str] = {
            "salescreate-api": "bad-release",
            "inventory-worker": "healthy",
            "notifier": "healthy",
        }
        self.custom_env_overrides: dict[str, dict[str, str]] = {
            "salescreate-api": {},
            "inventory-worker": {},
            "notifier": {},
        }

    def get_service_scenario(self, service: str) -> str:
        """Return the currently active fault scenario for a service."""
        svc = self._validate_service(service)
        return self.active_scenarios.get(svc, "healthy")

    def set_service_scenario(
        self,
        service: str,
        scenario: str,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Switch the active scenario for a microservice at runtime."""
        svc = self._validate_service(service)
        normalized = scenario.strip().lower()
        if svc in self._modules:
            scenarios_map = self._modules[svc]["SCENARIOS"]
            if normalized not in scenarios_map:
                available = ", ".join(scenarios_map.keys())
                raise ValueError(
                    f"Unknown scenario '{scenario}' for {svc}. Available: {available}"
                )

        self.active_scenarios[svc] = normalized
        if env_overrides is not None:
            self.custom_env_overrides[svc] = dict(env_overrides)
        else:
            self.custom_env_overrides[svc] = {}

        return {
            "service": svc,
            "scenario": normalized,
            "resolved_env": self.get_resolved_env(svc, normalized),
        }

    def get_baseline_env(self, service: str) -> dict[str, str]:
        """Return the healthy baseline environment dictionary for a service."""
        svc = self._validate_service(service)
        if svc in self._modules:
            return dict(self._modules[svc]["BASELINE_ENV"])
        return {
            "SERVICE_NAME": svc,
            "RELEASE_VERSION": "v1.0.0",
            "FAULT_MODE": "healthy",
            "LOG_LEVEL": "INFO",
        }

    def get_resolved_env(self, service: str, scenario: str | None = None) -> dict[str, str]:
        """Return the effective environment dictionary for a service and scenario."""
        svc = self._validate_service(service)
        target_scenario = scenario or self.active_scenarios.get(svc, "healthy")
        if svc in self._modules:
            scenarios_map = self._modules[svc]["SCENARIOS"]
            spec = scenarios_map.get(target_scenario)
            if spec is not None and hasattr(spec, "resolved_env"):
                env = dict(spec.resolved_env())
            elif spec is not None and hasattr(spec, "env"):
                env = dict(spec.env)
            else:
                env = dict(self._modules[svc]["BASELINE_ENV"])
                env["FAULT_MODE"] = target_scenario
        else:
            env = self.get_baseline_env(svc)
            env["FAULT_MODE"] = target_scenario
            if target_scenario != "healthy":
                env["RELEASE_VERSION"] = "v1.0.1-fault"

        if scenario is None and self.custom_env_overrides.get(svc):
            env.update(self.custom_env_overrides[svc])
        return env

    def list_available_scenarios(self, service: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """List all scenarios and their descriptions across one or all services."""
        targets = [self._validate_service(service)] if service else list(self.active_scenarios.keys())
        result: dict[str, list[dict[str, Any]]] = {}
        for svc in targets:
            if svc in self._modules:
                scenarios_map = self._modules[svc]["SCENARIOS"]
                svc_list: list[dict[str, Any]] = []
                for name, spec in scenarios_map.items():
                    title = getattr(spec, "title", None) or getattr(spec, "summary", name)
                    description = getattr(spec, "description", "")
                    remediation = (
                        getattr(spec, "recommended_remediation", None)
                        or getattr(spec, "remediation", "")
                    )
                    svc_list.append(
                        {
                            "name": name,
                            "title": title,
                            "description": description,
                            "remediation": remediation,
                            "active": self.active_scenarios.get(svc) == name,
                        }
                    )
                result[svc] = svc_list
            else:
                active_name = self.active_scenarios.get(svc, "healthy")
                result[svc] = [
                    {
                        "name": "healthy",
                        "title": "Healthy Baseline",
                        "description": f"Normal operation for {svc}.",
                        "remediation": "None required.",
                        "active": active_name == "healthy",
                    }
                ]
        return result

    def get_recent_logs(
        self,
        service: str,
        limit: int = 20,
        scenario: str | None = None,
        log_group_override: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pull recent structured JSON logs from the microservice telemetry generator or live CloudWatch."""
        svc = self._validate_service(service)
        target_scenario = scenario or self.active_scenarios.get(svc, "healthy")

        candidate_groups: list[str] = []
        if log_group_override:
            candidate_groups.append(log_group_override)
        for grp in (
            f"/ecs/{self.cluster}/{svc}",
            f"/ecs/cloud-ops-demo/{svc}",
            f"/ecs/{svc}",
        ):
            if grp not in candidate_groups:
                candidate_groups.append(grp)

        primary_group = log_group_override or f"/ecs/cloud-ops-demo/{svc}"
        cli_cmd = (
            f"aws logs filter-log-events --log-group-name {primary_group} "
            f"--limit {limit} --region {self.region}"
        )
        self.cli_guard.validate_command(cli_cmd, actor="investigator-agent")

        if self.live_aws and (scenario is None or scenario == self.active_scenarios.get(svc)):
            for grp in candidate_groups:
                live_events = self._try_fetch_live_cloudwatch_logs(grp, limit)
                if live_events:
                    latest_fault = live_events[-1].get("fault_mode")
                    if isinstance(latest_fault, str) and latest_fault.strip():
                        self.active_scenarios[svc] = latest_fault.strip()
                    return live_events
            # Live mode never falls back to synthetic telemetry: no events means no evidence.
            return []

        env = self.get_resolved_env(svc, target_scenario)
        now = datetime.now(timezone.utc)
        events: list[dict[str, Any]] = []

        if svc in self._modules:
            tick_fn = self._modules[svc]["generate_tick_event"]
            for idx in range(limit):
                ts_dt = now - timedelta(seconds=(limit - idx) * 3)
                ts_iso = ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if svc == "inventory-worker":
                    ev = dict(tick_fn(idx, env=env))
                    ev["timestamp"] = ts_iso
                else:
                    ev = dict(tick_fn(idx, env=env, timestamp=ts_iso))
                events.append(ev)
            return events

        # Dynamic external service fallback log synthesizer
        is_faulty = target_scenario != "healthy"
        for idx in range(limit):
            ts_dt = now - timedelta(seconds=(limit - idx) * 3)
            ts_iso = ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            is_err_tick = is_faulty and (idx % 2 == 0)
            events.append(
                {
                    "timestamp": ts_iso,
                    "service": svc,
                    "release": env.get("RELEASE_VERSION", "v1.0.0"),
                    "fault_mode": target_scenario,
                    "level": "ERROR" if is_err_tick else "INFO",
                    "status": "error" if is_err_tick else "ok",
                    "http_status": 503 if is_err_tick else 200,
                    "latency_ms": 1450 if is_err_tick else 42 + (idx % 15),
                    "error_code": "ServiceDegradedError" if is_err_tick else None,
                    "message": (
                        f"Service {svc} experienced fault_mode={target_scenario}"
                        if is_err_tick
                        else f"Processed request on {svc} normally"
                    ),
                    "metrics": {
                        "memory_rss_mb": 340 if is_err_tick else 135,
                        "queue_depth": 12 if is_err_tick else 1,
                        "dlq_count": 2 if is_err_tick else 0,
                        "db_pool_active": 18 if is_err_tick else 4,
                    },
                }
            )
        return events

    def get_service_metrics(
        self,
        service: str,
        window_ticks: int = 20,
        scenario: str | None = None,
    ) -> dict[str, Any]:
        """Compute error rate, p95 latency, memory RSS, and queue depth from structured service telemetry."""
        svc = self._validate_service(service)
        target_scenario = scenario or self.active_scenarios.get(svc, "healthy")
        logs = self.get_recent_logs(service=svc, limit=window_ticks, scenario=target_scenario)

        total = len(logs) or 1
        error_count = 0
        latencies: list[int] = []
        memory_values: list[int] = []
        queue_depths: list[int] = []
        dlq_counts: list[int] = []
        db_pool_values: list[int] = []
        timeseries: list[dict[str, Any]] = []

        running_errors = 0
        for idx, entry in enumerate(logs):
            level = str(entry.get("level", "INFO")).upper()
            http_status = int(entry.get("http_status", 200) or 200)
            status_field = str(entry.get("status", "ok")).lower()
            is_err = (
                level in ("ERROR", "CRITICAL")
                or http_status >= 400
                or status_field == "error"
            )
            if is_err:
                error_count += 1
                running_errors += 1

            lat = int(entry.get("latency_ms", 0) or 0)
            latencies.append(lat)

            m = entry.get("metrics") or {}
            rss = int(m.get("memory_rss_mb") or m.get("memory_used_mb") or 120)
            memory_values.append(rss)
            qd = int(m.get("queue_depth", 0) or 0)
            queue_depths.append(qd)
            dlq = int(m.get("dlq_count", 0) or 0)
            dlq_counts.append(dlq)
            db_pool = int(m.get("db_pool_active", 0) or 0)
            db_pool_values.append(db_pool)

            point_err_rate = round((running_errors / (idx + 1)) * 100.0, 1)
            timeseries.append(
                {
                    "tick": idx,
                    "timestamp": entry.get("timestamp", ""),
                    "error_rate_pct": point_err_rate,
                    "latency_ms": lat,
                    "memory_rss_mb": rss,
                    "queue_depth": qd,
                    "is_error": is_err,
                }
            )

        sorted_lat = sorted(latencies)
        p95_idx = min(len(sorted_lat) - 1, max(0, int(len(sorted_lat) * 0.95) - 1))
        p95_latency_ms = sorted_lat[p95_idx] if sorted_lat else 0
        avg_latency_ms = round(sum(latencies) / total, 1)
        max_latency_ms = max(latencies) if latencies else 0
        max_rss_mb = max(memory_values) if memory_values else 120
        max_queue_depth = max(queue_depths) if queue_depths else 0
        max_dlq_count = max(dlq_counts) if dlq_counts else 0
        max_db_pool = max(db_pool_values) if db_pool_values else 0
        error_rate_pct = round((error_count / total) * 100.0, 2)

        if target_scenario == "healthy" and error_rate_pct < 5.0:
            health_state = "HEALTHY"
        elif error_rate_pct >= 50.0 or max_rss_mb >= 480:
            health_state = "CRITICAL"
        else:
            health_state = "DEGRADED"

        release = logs[-1].get("release", "") if logs else ""
        return {
            "service": svc,
            "scenario": target_scenario,
            "release": release,
            "status": health_state,
            "sample_size": total,
            "log_events": len(logs),
            "latency_reported": any(bool(e.get("latency_ms")) for e in logs),
            "memory_reported": any(
                bool((e.get("metrics") or {}).get("memory_rss_mb") or (e.get("metrics") or {}).get("memory_used_mb"))
                for e in logs
            ),
            "error_count": error_count,
            "error_rate_pct": error_rate_pct,
            "avg_latency_ms": avg_latency_ms,
            "p95_latency_ms": p95_latency_ms,
            "max_latency_ms": max_latency_ms,
            "memory_rss_mb": max_rss_mb,
            "memory_limit_mb": 512,
            "queue_depth": max_queue_depth,
            "dlq_count": max_dlq_count,
            "db_pool_active": max_db_pool,
            "timeseries": timeseries,
        }

    def _try_fetch_live_cloudwatch_logs(
        self, log_group: str, limit: int
    ) -> list[dict[str, Any]]:
        try:
            proc = subprocess.run(
                [
                    "aws",
                    "logs",
                    "filter-log-events",
                    "--log-group-name",
                    log_group,
                    "--limit",
                    str(limit),
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
            parsed: list[dict[str, Any]] = []
            for raw_ev in payload.get("events", []):
                msg = raw_ev.get("message", "")
                try:
                    parsed.append(json.loads(msg))
                except Exception:
                    parsed.append({"message": msg, "level": "INFO"})
            return parsed
        except Exception:
            return []

    def _validate_service(self, service: str) -> str:
        svc = (service or "").strip()
        if not svc or not _SERVICE_NAME_RE.match(svc):
            raise ValueError(
                f"Invalid service identifier '{service}'. Must match ^[A-Za-z0-9_.-]+$"
            )
        if svc not in self.active_scenarios:
            self.active_scenarios[svc] = "healthy"
        self.custom_env_overrides.setdefault(svc, {})
        return svc
