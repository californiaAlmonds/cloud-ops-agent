"""Persistent JSON store for Incident Memory and Environment Memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_ops.config import ROOT_DIR
from cloud_ops.schemas import IncidentRecord, utc_now_iso

DEFAULT_PAST_INCIDENTS: list[dict[str, Any]] = [
    {
        "incident_id": "INC-2026-0912",
        "title": "Notifier SMTP Relay Auth Failure After Secrets Manager Rotation",
        "service": "notifier",
        "scenario": "secret-rotation-mismatch",
        "occurred_at": "2026-09-12T04:18:00Z",
        "error_signature": (
            "SecretAuthError: SMTP relay rejected credentials for SMTP_SECRET_ID=sec_2026_09_revoked"
        ),
        "root_cause": (
            "AWS Secrets Manager automatically rotated the SMTP relay credential to sec_2026_10_active, "
            "but the notifier ECS task definition remained pinned to revoked secret ID sec_2026_09_revoked."
        ),
        "resolution": (
            "Updated SMTP_SECRET_ID=sec_2026_10_active via update_service_env on notifier and forced a new deployment."
        ),
        "remediation_tool": "update_service_env",
        "mttr_minutes": 6,
        "verified": True,
        "tags": ["secrets-manager", "smtp", "notifier", "503"],
    },
    {
        "incident_id": "INC-2026-0928",
        "title": "SalesCreate API Checkout Pricing KeyError Regression in Canary Release",
        "service": "salescreate-api",
        "scenario": "bad-release",
        "occurred_at": "2026-09-28T14:42:00Z",
        "error_signature": (
            "KeyError: 'discount_tier' in checkout_pricing.calculate_tier_discount"
        ),
        "root_cause": (
            "Canary deployment v2.4.1-bad accessed payload['customer']['discount_tier'] directly "
            "without a default fallback, causing HTTP 500 errors on ~35% of guest/standard checkout orders."
        ),
        "resolution": (
            "Rolled back salescreate-api task definition from revision salescreate-api:15 (v2.4.1-bad) "
            "to healthy baseline revision salescreate-api:14 (v2.4.0)."
        ),
        "remediation_tool": "rollback_task_definition",
        "mttr_minutes": 4,
        "verified": True,
        "tags": ["canary", "rollback", "keyerror", "salescreate-api", "500"],
    },
    {
        "incident_id": "INC-2026-1004",
        "title": "Inventory Worker Row Lock Contention on Hot SKU-9901",
        "service": "inventory-worker",
        "scenario": "lock-contention",
        "occurred_at": "2026-10-04T19:05:00Z",
        "error_signature": (
            "DeadlockDetectedError: Pessimistic lock wait timeout (30ms) exceeded on table inventory_ledger for hot SKU-9901"
        ),
        "root_cause": (
            "Task definition override lowered LOCK_TIMEOUT_MS to 30ms (vs 1500ms baseline), causing "
            "60% of concurrent stock reservations on hot SKU-9901 to abort with DeadlockDetectedError."
        ),
        "resolution": (
            "Restored LOCK_TIMEOUT_MS=1500 via update_service_env on inventory-worker."
        ),
        "remediation_tool": "update_service_env",
        "mttr_minutes": 5,
        "verified": True,
        "tags": ["database", "lock-contention", "inventory-worker", "sku-9901"],
    },
]

DEFAULT_ENVIRONMENT_MEMORY: dict[str, Any] = {
    "cluster": "cloud-ops-demo-cluster",
    "region": "us-east-1",
    "dependency_chain": "salescreate-api -> inventory-worker -> notifier",
    "services": {
        "salescreate-api": {
            "tier": "tier-1-critical",
            "upstream": ["alb-public-ingress"],
            "downstream": ["inventory-worker"],
            "datastores": ["rds-sales-prod.internal:5432"],
            "baseline_revision": "salescreate-api:14",
            "baseline_release": "v2.4.0",
        },
        "inventory-worker": {
            "tier": "tier-1-worker",
            "upstream": ["salescreate-api"],
            "downstream": ["notifier", "erp-warehouse.internal"],
            "queues": ["sales-inventory-events.fifo"],
            "baseline_revision": "inventory-worker:9",
            "baseline_release": "v1.8.0",
        },
        "notifier": {
            "tier": "tier-2-notification",
            "upstream": ["inventory-worker"],
            "downstream": ["aws-ses", "hooks.slack-sim.internal"],
            "secrets": ["sec_2026_10_active"],
            "baseline_revision": "notifier:11",
            "baseline_release": "v3.1.0",
        },
    },
    "sla_targets": {
        "max_error_rate_pct": 1.0,
        "p95_latency_ms": 100,
        "memory_utilization_max_pct": 85.0,
    },
    "business_hours_policy": {
        "timezone": "IST (UTC+05:30)",
        "business_hours": {
            "window": "08:00-20:00 IST",
            "desired_count": 2,
            "description": (
                "High-availability active-active posture across 2 tasks during peak business hours."
            ),
        },
        "off_business_hours": {
            "window": "20:00-07:00 IST",
            "desired_count_prod": 1,
            "desired_count_free_plan": 0,
            "recommended_desired_count": 1,
            "description": (
                "Off-business hours cost-optimization policy (20:00-07:00 IST): scale down to "
                "desired_count=0 on AWS Free Plan / lab environments or desired_count=1 in production minimal standby."
            ),
        },
    },
}


class MemoryStore:
    """Manages Incident Memory and Environment Memory persisted in data/memory_store.json."""

    def __init__(self, store_path: Path | str | None = None) -> None:
        self.store_path = (
            Path(store_path)
            if store_path
            else (ROOT_DIR / "data" / "memory_store.json")
        )
        self.past_incidents: list[dict[str, Any]] = []
        self.environment_memory: dict[str, Any] = {}
        self.scaling_history: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        """Load memory state from disk or initialize with pre-seeded defaults."""
        if self.store_path.exists():
            try:
                raw = json.loads(self.store_path.read_text(encoding="utf-8"))
                self.past_incidents = list(raw.get("past_incidents", DEFAULT_PAST_INCIDENTS))
                self.environment_memory = dict(
                    raw.get("environment_memory", DEFAULT_ENVIRONMENT_MEMORY)
                )
                self.scaling_history = list(raw.get("scaling_history", []))
                if not self.past_incidents:
                    self.past_incidents = [dict(item) for item in DEFAULT_PAST_INCIDENTS]
                if not self.environment_memory:
                    self.environment_memory = dict(DEFAULT_ENVIRONMENT_MEMORY)
                return
            except Exception:
                pass

        self.past_incidents = [dict(item) for item in DEFAULT_PAST_INCIDENTS]
        self.environment_memory = json.loads(json.dumps(DEFAULT_ENVIRONMENT_MEMORY))
        self.scaling_history = []
        self.save()

    def save(self) -> None:
        """Persist current memory state to data/memory_store.json."""
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utc_now_iso(),
            "past_incidents": self.past_incidents,
            "environment_memory": self.environment_memory,
            "scaling_history": self.scaling_history,
        }
        self.store_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def find_similar_incidents(
        self,
        service: str,
        scenario: str = "",
        error_text: str = "",
    ) -> dict[str, Any] | None:
        """Find the best matching historical incident from Incident Memory."""
        scenario_norm = (scenario or "").strip().lower()
        error_norm = (error_text or "").strip().lower()

        best_match: dict[str, Any] | None = None
        best_score = 0.0

        for item in self.past_incidents:
            score = 0.0
            if item.get("service") == service:
                score += 0.45
            if scenario_norm and item.get("scenario", "").lower() == scenario_norm:
                score += 0.45

            sig = str(item.get("error_signature", "")).lower()
            rc = str(item.get("root_cause", "")).lower()
            if error_norm:
                for token in ("keyerror", "discount_tier", "secretautherror", "deadlock", "sku-9901", "timeout", "memory", "oom", "503", "502", "429"):
                    if token in error_norm and (token in sig or token in rc):
                        score += 0.15

            if score > best_score:
                best_score = score
                best_match = dict(item)
                best_match["similarity_score"] = round(min(0.99, score), 2)

        if best_match and best_score >= 0.45:
            return best_match
        return None

    def record_resolved_incident(self, incident: IncidentRecord) -> dict[str, Any]:
        """Store a newly verified incident resolution into Incident Memory and persist to disk."""
        inv = incident.investigation
        prop = incident.proposal
        entry = {
            "incident_id": incident.incident_id,
            "title": incident.title,
            "service": incident.service,
            "scenario": incident.scenario,
            "occurred_at": incident.created_at,
            "resolved_at": utc_now_iso(),
            "error_signature": (
                inv.sample_logs[0].get("message", incident.scenario)
                if (inv and inv.sample_logs)
                else incident.scenario
            ),
            "root_cause": inv.root_cause if inv else incident.title,
            "resolution": prop.title if prop else "Rolled back to healthy baseline.",
            "remediation_tool": prop.mcp_tool if prop else "rollback_task_definition",
            "mttr_minutes": 3,
            "verified": bool(incident.verification and incident.verification.verified),
            "tags": [incident.service, incident.scenario, "verified"],
        }

        # Replace if incident_id already exists, otherwise insert at head
        existing_ids = [idx for idx, x in enumerate(self.past_incidents) if x.get("incident_id") == incident.incident_id]
        if existing_ids:
            self.past_incidents[existing_ids[0]] = entry
        else:
            self.past_incidents.insert(0, entry)

        self.save()
        return entry

    def get_scaling_policy(
        self,
        period: str = "off_business_hours",
        free_plan: bool = False,
    ) -> dict[str, Any]:
        """Retrieve business-hours or off-business-hours scaling policy from Environment Memory."""
        policies = self.environment_memory.get("business_hours_policy", {})
        if period == "business_hours":
            bh = dict(policies.get("business_hours", {}))
            bh["period"] = "business_hours"
            bh["target_desired_count"] = int(bh.get("desired_count", 2))
            return bh

        obh = dict(policies.get("off_business_hours", {}))
        obh["period"] = "off_business_hours"
        obh["target_desired_count"] = (
            int(obh.get("desired_count_free_plan", 0))
            if free_plan
            else int(obh.get("desired_count_prod", 1))
        )
        return obh

    def get_service_topology(self, service: str) -> dict[str, Any]:
        """Return service dependencies and SLA targets from Environment Memory."""
        services_map = self.environment_memory.get("services", {})
        return {
            "service": service,
            "cluster": self.environment_memory.get("cluster", "cloud-ops-demo-cluster"),
            "dependency_chain": self.environment_memory.get(
                "dependency_chain", "salescreate-api -> inventory-worker -> notifier"
            ),
            "topology": services_map.get(service, {}),
            "sla_targets": self.environment_memory.get("sla_targets", {}),
        }

    def list_incidents(self) -> list[dict[str, Any]]:
        """Return list of past incidents in Incident Memory."""
        return list(self.past_incidents)

    def get_environment_memory(self) -> dict[str, Any]:
        """Return Environment Memory dict."""
        return dict(self.environment_memory)

    def get_summary(self) -> dict[str, Any]:
        """Return complete summary of Incident Memory and Environment Memory for dashboards."""
        return {
            "store_path": str(self.store_path),
            "past_incidents_count": len(self.past_incidents),
            "past_incidents": self.past_incidents,
            "environment_memory": self.environment_memory,
            "scaling_history": self.scaling_history,
        }
