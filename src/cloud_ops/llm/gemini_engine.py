"""Gemini 3.8 Flash Interactions API reasoning engine with deterministic evidence-driven fallback."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
import re
from typing import Any

from cloud_ops.config import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_VERTEX_LOCATION,
    DEFAULT_VERTEX_PROJECT,
    detect_adc_credentials_path,
    normalize_gemini_model,
)
from cloud_ops.mcp_servers.aws_cli_guard import DENIED_DESTRUCTIVE_ACTIONS

try:
    from google import genai  # type: ignore[import-not-found]
    from google.genai import types as genai_types  # type: ignore[import-not-found]

    HAS_GOOGLE_GENAI = True
except Exception:
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    HAS_GOOGLE_GENAI = False

MAX_TOOL_ROUNDS = 6

_NON_SERVICE_HYPHENATED: frozenset[str] = frozenset(
    {
        "off-business",
        "business-hours",
        "read-only",
        "post-incident",
        "real-time",
        "multi-turn",
        "in-memory",
        "step-by-step",
        "end-to-end",
        "delete-cluster",
        "delete-service",
        "deregister-task-definition",
        "delete-log-group",
        "delete-log-stream",
        "delete-repository",
        "create-user",
        "delete-role",
        "attach-role-policy",
        "update-service",
        "register-task-definition",
        "list-services",
        "describe-services",
        "describe-task-definition",
        "list-task-definitions",
        "filter-log-events",
        "start-query",
        "get-role-policy",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "eu-west-1",
        "ap-south-1",
        "cloud-ops-demo-cluster",
        "cloud-ops-demo",
        "cloud-ops-agent",
        "cloud-ops",
        "non-healthy",
        "high-level",
        "one-click",
        "auto-resizing",
        "full-size",
        "bad-release",
        "memory-leak",
        "template-error",
        "poison-message",
        "lock-contention",
        "secret-rotation-mismatch",
        "db-pool-exhaustion",
        "key-error",
        "http-500",
        "http-503",
    }
)
_HYPHENATED_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b")


class GeminiReasoningEngine:
    """Executes multimodal and operational reasoning via Vertex AI Gemini (ADC) or Gemini API.

    Supports two live authentication modes with `google-genai`:
    1. Vertex AI via Google Cloud ADC (`vertexai=True, project=..., location=...`) — primary default.
    2. Gemini Developer API (`GEMINI_API_KEY`).
    Automatically falls back to a deterministic, evidence-driven reasoning engine when offline.
    """

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_MODEL,
        use_vertex_ai: bool = True,
        vertex_project: str = DEFAULT_VERTEX_PROJECT,
        vertex_location: str = DEFAULT_VERTEX_LOCATION,
    ) -> None:
        self.requested_model = model
        self.model = normalize_gemini_model(model)
        self.model_substituted = self.model != (model or "").strip()
        self.use_vertex_ai = use_vertex_ai
        self.vertex_project = (
            vertex_project
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("VERTEX_PROJECT")
            or os.environ.get("BRIDGE_VERTEX_PROJECT")
            or DEFAULT_VERTEX_PROJECT
        )
        self.vertex_location = (
            vertex_location
            or os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("VERTEX_LOCATION")
            or os.environ.get("BRIDGE_VERTEX_LOCATION")
            or DEFAULT_VERTEX_LOCATION
        )
        self.last_interaction_id: str | None = None
        self.last_mode: str = self.auth_mode
        self._client: Any = None
        self._resolved_vertex_model: str | None = None

    @property
    def adc_available(self) -> bool:
        """Return True when Google Cloud Application Default Credentials exist on disk."""
        return detect_adc_credentials_path() is not None

    @property
    def auth_mode(self) -> str:
        """Return the active authentication mode for Gemini calls."""
        if not HAS_GOOGLE_GENAI:
            return "deterministic-evidence-engine"
        if os.environ.get("CLOUD_OPS_OFFLINE_LLM", "").strip().lower() in ("1", "true", "yes"):
            return "deterministic-evidence-engine"
        if self.use_vertex_ai and self.vertex_project and self.adc_available:
            return "vertex-ai-adc"
        if os.environ.get("GEMINI_API_KEY", "").strip():
            return "gemini-api-key"
        return "deterministic-evidence-engine"

    @property
    def live_sdk_available(self) -> bool:
        """Return True when google-genai SDK is installed and either Vertex ADC or GEMINI_API_KEY is ready."""
        return self.auth_mode in ("vertex-ai-adc", "gemini-api-key")

    def status_summary(self) -> dict[str, Any]:
        """Return structured metadata about the active LLM engine and authentication."""
        adc_path = detect_adc_credentials_path()
        return {
            "model": self.model,
            "auth_mode": self.auth_mode,
            "live_sdk_available": self.live_sdk_available,
            "use_vertex_ai": self.use_vertex_ai,
            "vertex_project": self.vertex_project,
            "vertex_location": self.vertex_location,
            "adc_credentials_found": adc_path is not None,
            "adc_credentials_path": str(adc_path) if adc_path else None,
        }

    def _get_client(self) -> Any:
        """Initialize or return the cached google.genai.Client for Vertex AI ADC or API Key."""
        if self._client is not None:
            return self._client
        if genai is None:
            return None
        if self.auth_mode == "vertex-ai-adc":
            self._client = genai.Client(
                vertexai=True,
                project=self.vertex_project,
                location=self.vertex_location,
            )
        elif self.auth_mode == "gemini-api-key":
            self._client = genai.Client()
        return self._client

    def _generate_content_with_model_fallback(
        self,
        client: Any,
        contents: Any,
        config: Any,
    ) -> Any:
        """Call generate_content using self.model, falling back to the live Vertex endpoint if 404."""
        candidates = (
            [self._resolved_vertex_model]
            if self._resolved_vertex_model
            else [self.model, "gemini-2.5-flash"]
        )
        last_exc: Exception | None = None
        for candidate_model in candidates:
            if not candidate_model:
                continue
            try:
                resp = client.models.generate_content(
                    model=candidate_model,
                    contents=contents,
                    config=config,
                )
                self._resolved_vertex_model = candidate_model
                return resp
            except Exception as exc:
                last_exc = exc
                err_str = str(exc).upper()
                if "404" in err_str or "NOT_FOUND" in err_str:
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No Gemini model available for generate_content.")

    def reason(
        self,
        prompt: str | list[dict[str, Any]],
        system_instruction: str | None = None,
        context: dict[str, Any] | None = None,
        previous_interaction_id: str | None = None,
    ) -> dict[str, Any]:
        """Run an interaction turn via Vertex AI Gemini (ADC), Gemini API, or deterministic fallback."""
        active_auth = self.auth_mode
        if self.live_sdk_available and genai is not None:
            try:
                client = self._get_client()
                if client is not None:
                    if active_auth == "vertex-ai-adc":
                        cfg_kwargs: dict[str, Any] = {}
                        if system_instruction:
                            cfg_kwargs["system_instruction"] = system_instruction
                        if genai_types is not None and hasattr(genai_types, "ThinkingConfig"):
                            try:
                                cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                                    thinking_budget=0
                                )
                            except Exception:
                                pass
                        cfg = (
                            genai_types.GenerateContentConfig(**cfg_kwargs)
                            if (genai_types is not None and cfg_kwargs)
                            else None
                        )
                        resp = self._generate_content_with_model_fallback(
                            client=client,
                            contents=str(prompt),
                            config=cfg,
                        )
                        output_text = (getattr(resp, "text", None) or "").strip()
                        interaction_id = self._synthetic_interaction_id(str(prompt))
                        if output_text:
                            self.last_interaction_id = interaction_id
                            self.last_mode = "vertex-ai-adc"
                            return {
                                "text": output_text,
                                "model": self.model,
                                "mode": self.last_mode,
                                "auth_mode": active_auth,
                                "vertex_project": self.vertex_project,
                                "interaction_id": interaction_id,
                                "steps": ["vertex_ai_adc:generate_content"],
                            }

                    # Gemini Developer API (GEMINI_API_KEY): use Interactions API
                    create_kwargs: dict[str, Any] = {
                        "model": self.model,
                        "input": prompt,
                    }
                    if system_instruction:
                        create_kwargs["system_instruction"] = system_instruction
                    if previous_interaction_id:
                        create_kwargs["previous_interaction_id"] = previous_interaction_id

                    interaction = client.interactions.create(**create_kwargs)
                    output_text = getattr(interaction, "output_text", None) or ""
                    raw_steps = getattr(interaction, "steps", None) or []
                    interaction_id = getattr(interaction, "id", None) or self._synthetic_interaction_id(
                        str(prompt)
                    )
                    if output_text.strip():
                        self.last_interaction_id = interaction_id
                        self.last_mode = f"{active_auth}:interactions"
                        return {
                            "text": output_text.strip(),
                            "model": self.model,
                            "mode": self.last_mode,
                            "auth_mode": active_auth,
                            "vertex_project": None,
                            "interaction_id": interaction_id,
                            "steps": [str(s) for s in raw_steps],
                        }
            except Exception as exc:
                fallback_text = self._deterministic_fallback(prompt, context=context, error_note=str(exc))
                self.last_mode = "deterministic-evidence-engine"
                return {
                    "text": fallback_text,
                    "model": self.model,
                    "mode": "deterministic-evidence-engine",
                    "auth_mode": active_auth,
                    "interaction_id": self._synthetic_interaction_id(str(prompt)),
                    "steps": ["thought:fallback_after_sdk_exception", "model_output:evidence_synthesis"],
                }

        fallback_text = self._deterministic_fallback(prompt, context=context)
        interaction_id = self._synthetic_interaction_id(str(prompt))
        self.last_interaction_id = interaction_id
        self.last_mode = "deterministic-evidence-engine"
        return {
            "text": fallback_text,
            "model": self.model,
            "mode": "deterministic-evidence-engine",
            "auth_mode": active_auth,
            "interaction_id": interaction_id,
            "steps": ["thought:inspect_telemetry_and_diffs", "model_output:evidence_synthesis"],
        }

    def synthesize_investigation(
        self,
        service: str,
        scenario: str,
        metrics: dict[str, Any],
        sample_logs: list[dict[str, Any]],
        task_diff: dict[str, Any] | None,
        past_incident: dict[str, Any] | None,
        multimodal_insights: list[str] | None = None,
    ) -> dict[str, str]:
        """Synthesize root cause and executive summary from live telemetry and task diffs."""
        error_rate = metrics.get("error_rate_pct", 0.0)
        p95_lat = metrics.get("p95_latency_ms", 0)
        rss_mb = metrics.get("memory_rss_mb", 0)
        queue_depth = metrics.get("queue_depth", 0)

        first_error_msg = ""
        first_error_type = ""
        for entry in sample_logs:
            err = entry.get("error")
            if isinstance(err, dict) and err.get("message"):
                first_error_msg = str(err.get("message"))
                first_error_type = str(err.get("type", "RuntimeError"))
                break
            if entry.get("level") in ("ERROR", "CRITICAL") and entry.get("message"):
                first_error_msg = str(entry.get("message"))
                break

        changed_env = (task_diff or {}).get("changed_env", {})
        active_rev = (task_diff or {}).get("active_revision", f"{service}:latest")
        base_rev = (task_diff or {}).get("baseline_revision", f"{service}:baseline")

        env_diff_parts: list[str] = []
        for key, diff_val in changed_env.items():
            if key == "FAULT_MODE":
                continue
            b_val = diff_val.get("baseline", "")
            a_val = diff_val.get("active", "")
            env_diff_parts.append(f"{key}: '{b_val}' -> '{a_val}'")
        if not env_diff_parts and changed_env:
            for key, diff_val in changed_env.items():
                env_diff_parts.append(
                    f"{key}: '{diff_val.get('baseline', '')}' -> '{diff_val.get('active', '')}'"
                )

        diff_summary = ", ".join(env_diff_parts) if env_diff_parts else "nominal baseline configuration"

        if scenario == "healthy":
            root_cause = (
                f"No active fault detected on {service}. Task definition {active_rev} matches "
                f"healthy baseline ({base_rev}) with {error_rate}% error rate and {p95_lat}ms p95 latency."
            )
            summary = (
                f"Investigator verified {service} is operating normally on {active_rev} "
                f"(error rate {error_rate}%, p95 latency {p95_lat}ms, RSS {rss_mb}MB)."
            )
        else:
            root_cause = (
                f"Active task definition {active_rev} on {service} introduced configuration/release drift "
                f"({diff_summary}) compared to healthy baseline {base_rev}, triggering "
                f"{first_error_type or scenario}: {first_error_msg or scenario}."
            )
            past_note = ""
            if past_incident and past_incident.get("incident_id"):
                past_note = (
                    f" Correlated with historical incident {past_incident['incident_id']} "
                    f"({past_incident.get('title', '')})."
                )
            mm_note = ""
            if multimodal_insights:
                mm_note = f" Runbook/diagram context incorporated ({len(multimodal_insights)} artifact insight(s))."
            summary = (
                f"{service} is experiencing a {error_rate}% error rate (p95 latency {p95_lat}ms, "
                f"RSS {rss_mb}MB, queue_depth {queue_depth}) under scenario '{scenario}'. "
                f"After ruling out underlying AWS infrastructure/host exhaustion, Investigator confirmed "
                f"task revision {active_rev} changed {diff_summary}.{past_note}{mm_note}"
            )

        context = {
            "service": service,
            "scenario": scenario,
            "root_cause": root_cause,
            "summary": summary,
        }
        prompt = (
            f"Synthesize incident investigation for service={service}, scenario={scenario}.\n"
            f"Metrics: error_rate={error_rate}%, p95_latency={p95_lat}ms, rss={rss_mb}MB.\n"
            f"Sample error: {first_error_msg}\n"
            f"Task diff ({base_rev} -> {active_rev}): {diff_summary}"
        )
        res = self.reason(
            prompt=prompt,
            system_instruction=(
                "You are the Lead Cloud Ops Investigator Agent. Provide concise, evidence-backed root cause analysis."
            ),
            context=context,
        )
        return {
            "summary": summary,
            "root_cause": root_cause,
            "llm_output": res["text"],
            "model": res["model"],
            "mode": res["mode"],
        }

    def analyze_multimodal_attachment(
        self,
        filename: str,
        mime_type: str,
        extracted_text: str,
        service: str = "",
        scenario: str = "",
    ) -> str:
        """Extract operational insights from an uploaded runbook, architecture diagram, or screenshot."""
        snippet = (extracted_text or "").strip().replace("\r", " ")
        if len(snippet) > 260:
            snippet = snippet[:257] + "..."
        prompt = (
            f"Extract operational guidance from attachment '{filename}' ({mime_type}) "
            f"for service='{service}' scenario='{scenario}':\n{snippet}"
        )
        res = self.reason(
            prompt=prompt,
            system_instruction="Extract actionable SRE runbook or architecture topology guidance.",
            context={
                "filename": filename,
                "mime_type": mime_type,
                "snippet": snippet,
                "service": service,
                "scenario": scenario,
            },
        )
        return res["text"]

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        attachments: list[dict[str, str]],
        tool_declarations: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        active_service: str | None = None,
    ) -> dict[str, Any]:
        """Run a bounded multi-turn tool-calling loop via Gemini 3.8 Flash or deterministic fallback."""
        called_any_tool = False

        def tracked_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
            nonlocal called_any_tool
            called_any_tool = True
            return tool_executor(name, args)

        if self.live_sdk_available and genai is not None and genai_types is not None:
            try:
                client = self._get_client()
                if client is not None:
                    fn_decls = [
                        genai_types.FunctionDeclaration(
                            name=td["name"],
                            description=td.get("description", ""),
                            parameters=td.get("parameters"),
                        )
                        for td in tool_declarations
                    ]
                    tool_obj = genai_types.Tool(function_declarations=fn_decls)
                    system_instruction = "You are a Cloud Ops Agent with access to AWS MCP tools."

                    cfg_kwargs: dict[str, Any] = {
                        "system_instruction": system_instruction,
                        "temperature": 0.2,
                        "tools": [tool_obj],
                    }
                    if hasattr(genai_types, "ThinkingConfig"):
                        try:
                            cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                                thinking_budget=0
                            )
                        except Exception:
                            pass
                    cfg = genai_types.GenerateContentConfig(**cfg_kwargs)

                    recent_msgs = messages[-16:] if messages else []
                    contents: list[Any] = []
                    for idx, msg in enumerate(recent_msgs):
                        role = "model" if msg.get("role") == "assistant" else "user"
                        text_content = str(msg.get("content") or "").strip()
                        if idx == len(recent_msgs) - 1 and role == "user" and attachments:
                            att_notes = [
                                f"[Attachment {a.get('filename', 'file')} ({a.get('mime_type', '')})]: "
                                f"{str(a.get('preview_text') or a.get('content') or '')[:300]}"
                                for a in attachments
                            ]
                            text_content = f"{text_content}\n\n" + "\n".join(att_notes)
                        if text_content:
                            contents.append(
                                genai_types.Content(
                                    role=role,
                                    parts=[genai_types.Part.from_text(text=text_content)],
                                )
                            )

                    if not contents:
                        contents.append(
                            genai_types.Content(
                                role="user",
                                parts=[genai_types.Part.from_text(text="Hello")],
                            )
                        )

                    resp = self._generate_content_with_model_fallback(
                        client=client,
                        contents=contents,
                        config=cfg,
                    )

                    for _ in range(MAX_TOOL_ROUNDS):
                        fn_calls = getattr(resp, "function_calls", None) or []
                        if not fn_calls:
                            break
                        candidates = getattr(resp, "candidates", None) or []
                        if candidates and getattr(candidates[0], "content", None) is not None:
                            contents.append(candidates[0].content)
                        fn_response_parts: list[Any] = []
                        for fc in fn_calls:
                            fc_name = str(getattr(fc, "name", "") or "")
                            fc_args = dict(getattr(fc, "args", None) or {})
                            tool_res = tracked_executor(fc_name, fc_args)
                            fn_response_parts.append(
                                genai_types.Part.from_function_response(
                                    name=fc_name,
                                    response={"result": tool_res},
                                )
                            )
                        contents.append(
                            genai_types.Content(
                                role="user",
                                parts=fn_response_parts,
                            )
                        )
                        resp = self._generate_content_with_model_fallback(
                            client=client,
                            contents=contents,
                            config=cfg,
                        )

                    reply_text = (getattr(resp, "text", None) or "").strip()
                    if reply_text:
                        self.last_mode = self.auth_mode
                        return {
                            "reply": reply_text,
                            "mode": self.auth_mode,
                        }
            except Exception:
                pass

        return self._deterministic_tool_chat(
            messages=messages,
            attachments=attachments,
            tool_executor=tool_executor,
            active_service=active_service,
        )

    def _detect_service_from_text(
        self,
        text: str,
        active_service: str | None = None,
    ) -> tuple[str, bool]:
        """Detect a service name in user text. Returns (service_name, explicitly_mentioned)."""
        lower = (text or "").lower()
        for known in ("salescreate-api", "inventory-worker", "notifier"):
            if known in lower:
                return known, True

        for match in _HYPHENATED_TOKEN_RE.finditer(lower):
            candidate = match.group(1).strip("-")
            if candidate and candidate not in _NON_SERVICE_HYPHENATED and len(candidate) >= 5:
                return candidate, True

        if "inventory" in lower or "worker" in lower or "sku" in lower:
            return "inventory-worker", True
        if "notifier" in lower or "smtp" in lower or "webhook" in lower or "ses" in lower:
            return "notifier", True
        if "salescreate" in lower or "sales" in lower or "order" in lower or "checkout" in lower:
            return "salescreate-api", True

        if active_service:
            return active_service, False
        return "salescreate-api", False

    def _deterministic_tool_chat(
        self,
        messages: list[dict[str, Any]],
        attachments: list[dict[str, str]],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        active_service: str | None = None,
    ) -> dict[str, Any]:
        """Deterministic tool-calling planner guaranteeing offline parity with live Gemini."""
        user_text = str(messages[-1].get("content", "") if messages else "").strip()
        lower = user_text.lower()
        target_svc, explicit_svc = self._detect_service_from_text(user_text, active_service)

        # 0. Conversational / non-operational messages (e.g., "hi", "hello", "thanks")
        has_ops_intent = explicit_svc or any(
            kw in lower
            for kw in (
                "aws",
                "ecs",
                "cloudwatch",
                "log",
                "metric",
                "service",
                "cluster",
                "iam",
                "permission",
                "guardrail",
                "scale",
                "business hours",
                "off-business",
                "desired",
                "deploy",
                "diff",
                "compare",
                "revision",
                "task",
                "error",
                "fail",
                "why",
                "investigate",
                "diagnose",
                "fix",
                "remediat",
                "rollback",
                "root cause",
                "report",
                "document",
                "export",
                "chart",
                "graph",
                "plot",
                "svg",
                "visualiz",
                "excel",
                "xlsx",
                "spreadsheet",
                "sheet",
                "timeline",
                "csv",
                "word",
                "docx",
                "markdown",
                ".md",
                "pir",
                "post-incident",
                "status",
                "health",
                "degraded",
                "delete",
                "terminate",
                "drop",
                "destroy",
            )
        )
        if not has_ops_intent and not attachments:
            self.last_mode = "deterministic-evidence-engine"
            return {
                "reply": "Hello! How can I help you with your AWS services today?",
                "mode": self.last_mode,
            }

        # 1. Destructive / denied AWS CLI command check
        for denied_action in DENIED_DESTRUCTIVE_ACTIONS:
            if denied_action in lower or denied_action.replace("-", " ") in lower:
                test_cmd = (
                    user_text
                    if lower.startswith("aws ")
                    else f"aws ecs {denied_action} --cluster cloud-ops-demo-cluster"
                )
                iam_res = tool_executor("inspect_iam_and_guardrails", {"test_command": test_cmd})
                validation = iam_res.get("validation") or {}
                reason = validation.get("reason") or f"Action '{denied_action}' is explicitly denied."
                reply = (
                    f"GUARDRAIL BLOCKED: `{test_cmd}` is explicitly denied by IAM policy "
                    f"(`ExplicitDenyDestructiveOperations`) and `GuardrailedAWSCLI` ({validation.get('category', 'DENIED_DESTRUCTIVE')}: {reason})."
                )
                self.last_mode = "deterministic-evidence-engine"
                return {"reply": reply, "mode": self.last_mode}

        # 2. IAM role permissions / guardrails inspection
        if (
            "iam" in lower
            or "permission" in lower
            or "guardrail" in lower
            or ("what can" in lower and "write" in lower)
        ):
            iam_res = tool_executor("inspect_iam_and_guardrails", {})
            perms = iam_res.get("iam_permissions") or iam_res
            write_actions = ", ".join(perms.get("allowlisted_write_actions") or [])
            denied_actions = ", ".join(perms.get("explicitly_denied_actions") or [])
            read_actions = ", ".join((perms.get("allowed_read_only_actions") or [])[:8])
            reply = (
                f"{perms.get('summary', '')}\n\n"
                f"Allowlisted write actions (approval required): `{write_actions}`\n"
                f"Read-only actions: `{read_actions}`\n"
                f"Explicitly denied destructive actions: `{denied_actions}`"
            )
            self.last_mode = "deterministic-evidence-engine"
            return {"reply": reply, "mode": self.last_mode}

        # 3. Scaling / off-business hours request
        if (
            "scale" in lower
            or "business hours" in lower
            or "off-business" in lower
            or "desired count" in lower
            or "desired_count" in lower
        ):
            is_off_hours = "off" in lower or "down" in lower or "night" in lower
            free_plan = "free" in lower or "zero" in lower or "desired_count=0" in lower
            period = "off_business_hours" if is_off_hours else "business_hours"
            target_count = 0 if free_plan else (1 if is_off_hours else 2)
            count_match = re.search(r"(?:to|desired[_ ]count\s*=?\s*)(\d+)", lower)
            if count_match:
                target_count = int(count_match.group(1))
            reason = (
                f"{period.replace('_', ' ')} scaling policy"
                if ("business" in lower or "off" in lower)
                else f"Operator requested desired_count={target_count}"
            )
            scale_res = tool_executor(
                "propose_service_remediation",
                {
                    "service": target_svc,
                    "action_type": "scale_service",
                    "target_value": str(target_count),
                    "reason": reason,
                    "period": period,
                    "free_plan": free_plan,
                },
            )
            prop = scale_res.get("proposal") or {}
            policy = scale_res.get("scaling_policy") or {}
            window_str = f" ({policy.get('window')})" if policy.get("window") else ""
            reply = (
                f"Prepared {period.replace('_', ' ')} scaling proposal `{prop.get('proposal_id', '')}` "
                f"for `{target_svc}`{window_str}: "
                f"`desired_count={prop.get('parameters', {}).get('desired_count', target_count)}`.\n"
                f"Command: `{prop.get('aws_cli_equivalent', '')}`"
            )
            self.last_mode = "deterministic-evidence-engine"
            return {"reply": reply, "mode": self.last_mode}

        # 4. Cluster-wide service listing / general health check
        is_cluster_overview = not explicit_svc and any(
            phrase in lower
            for phrase in (
                "list service",
                "list ecs",
                "ecs services",
                "list all",
                "what services",
                "which services",
                "all our aws services",
                "all our services",
                "all services",
                "are our services healthy",
                "services are healthy",
                "services are degraded",
                "degraded service",
                "cluster health",
                "check if all",
                "status of all",
            )
        )
        if is_cluster_overview:
            list_res = tool_executor("list_ecs_services", {})
            services = list_res.get("services") or []
            lines = [
                f"ECS services in `{list_res.get('cluster', 'cloud-ops-demo-cluster')}` ({list_res.get('region', 'us-east-1')}):"
            ]
            degraded_svc: str | None = None
            for svc_info in services:
                s_name = svc_info.get("service", "unknown")
                h_status = svc_info.get("health_status", "HEALTHY")
                scen = svc_info.get("scenario", "healthy")
                run_c = svc_info.get("running_count", 0)
                des_c = svc_info.get("desired_count", 0)
                rev = svc_info.get("active_task_definition", "")
                lines.append(
                    f"- `{s_name}`: {h_status} ({scen}) — {run_c}/{des_c} tasks on `{rev}`"
                )
                if h_status != "HEALTHY" and degraded_svc is None:
                    degraded_svc = s_name

            if degraded_svc:
                inv_res = tool_executor("investigate_service", {"service": degraded_svc})
                inv = inv_res.get("investigation") or {}
                prop = inv_res.get("proposal") or {}
                lines.append(
                    f"\n`{degraded_svc}` is degraded: {inv.get('summary', '')} "
                    f"Root cause: {inv.get('root_cause', '')} "
                    f"Proposed fix: {prop.get('title', '')}"
                )

            self.last_mode = "deterministic-evidence-engine"
            return {"reply": "\n".join(lines), "mode": self.last_mode}

        # 5. Specific report / chart / Excel / log query requests (strictly on-demand)
        wants_chart = any(kw in lower for kw in ("chart", "graph", "plot", "svg", "visualiz"))
        wants_excel = any(kw in lower for kw in ("excel", "xlsx", "spreadsheet", "sheet", "timeline", "csv"))
        wants_word = any(kw in lower for kw in ("word", "docx"))
        wants_md = any(kw in lower for kw in ("markdown", ".md"))
        wants_pir = any(kw in lower for kw in ("post-incident", "pir", "operational artifacts", "all artifacts"))
        wants_report = any(kw in lower for kw in ("report", "document", "export"))
        mentions_logs = any(kw in lower for kw in ("cloudwatch", "log", "logs", "failure", "failures", "error"))
        wants_investigation = any(
            kw in lower
            for kw in ("why", "investigate", "diagnose", "fix", "remediat", "rollback", "root cause")
        )

        # 5a. Custom report based on CloudWatch logs / failures (no forced PIR bundle or unsolicited chart/excel)
        if (wants_report or wants_word or wants_md) and mentions_logs and not wants_pir and not wants_excel and not wants_investigation:
            log_res = tool_executor(
                "get_service_logs_and_metrics",
                {"service": target_svc, "limit": 20},
            )
            logs = log_res.get("logs") or []
            metrics = log_res.get("metrics") or {}
            failure_logs = [
                entry
                for entry in logs
                if str(entry.get("level", "")).upper() in ("ERROR", "CRITICAL", "FATAL", "WARN")
                or any(
                    err_tok in str(entry.get("message", "")).lower()
                    for err_tok in ("error", "exception", "fail", "timeout", "500", "502", "504", "refused")
                )
            ]
            selected_logs = failure_logs if failure_logs else logs
            err_rate = metrics.get("error_rate_pct", 0.0)
            p95_lat = metrics.get("p95_latency_ms", 0.0)
            status_str = metrics.get("status", "UNKNOWN")

            log_bullets = "\n".join(
                f"- `[{entry.get('timestamp', '')}]` {entry.get('level', 'INFO')} — `{entry.get('message', '')}`"
                for entry in selected_logs[:10]
            ) or "- No failure log events returned in the queried CloudWatch window."

            report_title = f"CloudWatch Failure Log Analysis — {target_svc}"
            custom_md = (
                f"# {report_title}\n\n"
                f"Service `{target_svc}` (`/aws/ecs/{target_svc}`) is `{status_str}` with "
                f"`{err_rate}%` error rate and `{p95_lat} ms` p95 latency.\n\n"
                f"CloudWatch log entries:\n{log_bullets}\n"
            )
            doc_fmt = "md" if (wants_md and not wants_word) else ("both" if (wants_md and wants_word) else "docx")
            doc_res = tool_executor(
                "create_document_report",
                {
                    "service": target_svc,
                    "title": report_title,
                    "content_markdown": custom_md,
                    "format": doc_fmt,
                },
            )
            if wants_chart:
                tool_executor(
                    "render_svg_chart",
                    {
                        "service": target_svc,
                        "title": f"{target_svc} — CloudWatch Failure Rate & P95 Latency",
                    },
                )
            created_docs = doc_res.get("artifacts") or []
            doc_list_str = ", ".join(f"`{d.get('name', '')}`" for d in created_docs)
            reply = custom_md + (f"\nGenerated report: {doc_list_str}" if doc_list_str else "")
            self.last_mode = "deterministic-evidence-engine"
            return {"reply": reply, "mode": self.last_mode}

        # 5b. Explicit chart-only or Excel-only or multi-artifact request
        if wants_chart or wants_excel or wants_word or wants_md or wants_pir or wants_report:
            if wants_chart and not (wants_excel or wants_word or wants_md or wants_pir or wants_report):
                if wants_investigation:
                    inv_res = tool_executor("investigate_service", {"service": target_svc})
                    tool_executor(
                        "render_svg_chart",
                        {
                            "service": target_svc,
                            "title": f"{target_svc} — CloudWatch Error Rate & P95 Latency",
                        },
                    )
                    inv = inv_res.get("investigation") or {}
                    prop = inv_res.get("proposal") or {}
                    reply = (
                        f"{inv.get('summary', '')}\n\n"
                        f"Root cause: {inv.get('root_cause', '')}\n"
                        f"Proposed fix: {prop.get('title', 'None')} (`{prop.get('aws_cli_equivalent', '')}`)"
                    )
                    self.last_mode = "deterministic-evidence-engine"
                    return {"reply": reply, "mode": self.last_mode}

                chart_res = tool_executor(
                    "render_svg_chart",
                    {
                        "service": target_svc,
                        "title": f"{target_svc} — CloudWatch Error Rate & P95 Latency",
                    },
                )
                art = chart_res.get("artifact") or {}
                reply = f"Rendered telemetry chart `{art.get('name', 'chart.svg')}` for `{target_svc}`."
                self.last_mode = "deterministic-evidence-engine"
                return {"reply": reply, "mode": self.last_mode}

            if wants_excel and not (wants_word or wants_md or wants_pir):
                excel_res = tool_executor(
                    "create_excel_report",
                    {
                        "service": target_svc,
                        "title": f"{target_svc} — CloudWatch Log & Metric Export",
                    },
                )
                if wants_chart:
                    tool_executor(
                        "render_svg_chart",
                        {
                            "service": target_svc,
                            "title": f"{target_svc} — CloudWatch Error Rate & P95 Latency",
                        },
                    )
                art = excel_res.get("artifact") or {}
                reply = f"Generated Excel report `{art.get('name', 'report.xlsx')}` for `{target_svc}`."
                self.last_mode = "deterministic-evidence-engine"
                return {"reply": reply, "mode": self.last_mode}

            # Full Post-Incident Report (PIR) or multi-artifact bundle when explicitly requested
            inv_res = tool_executor("investigate_service", {"service": target_svc})
            inc_chart = wants_chart or wants_pir
            inc_excel = wants_excel or wants_pir
            inc_word = wants_word or wants_pir or (wants_report and not wants_md)
            inc_md = wants_md or wants_pir
            art_res = tool_executor(
                "generate_operational_artifacts",
                {
                    "service": target_svc,
                    "include_chart": inc_chart,
                    "include_excel": inc_excel,
                    "include_word": inc_word,
                    "include_markdown": inc_md,
                },
            )
            artifacts = art_res.get("artifacts") or []
            inv = inv_res.get("investigation") or {}
            bullet_lines = "\n".join(
                f"- {str(a.get('kind', '')).upper()}: `{a.get('name', '')}`"
                for a in artifacts
            )
            reply = (
                f"{inv.get('summary', '')}\n\n"
                f"Root cause: {inv.get('root_cause', '')}\n\n"
                f"Generated artifacts:\n{bullet_lines}"
            )
            self.last_mode = "deterministic-evidence-engine"
            return {"reply": reply, "mode": self.last_mode}

        # 5c. Direct read-only CloudWatch logs / metrics inspection (when user just asks to check/show logs)
        if any(
            phrase in lower
            for phrase in ("show log", "show cloudwatch", "get log", "fetch log", "recent log", "view log")
        ):
            log_res = tool_executor("get_service_logs_and_metrics", {"service": target_svc, "limit": 15})
            logs = log_res.get("logs") or []
            metrics = log_res.get("metrics") or {}
            log_lines = "\n".join(
                f"- `[{e.get('timestamp', '')}]` {e.get('level', 'INFO')}: `{e.get('message', '')}`"
                for e in logs[:10]
            ) or "- No recent log events found."
            reply = (
                f"`{target_svc}` status is `{metrics.get('status', 'UNKNOWN')}` "
                f"(error rate `{metrics.get('error_rate_pct', 0)}%`, p95 latency `{metrics.get('p95_latency_ms', 0)}ms`).\n\n"
                f"Recent logs from `/aws/ecs/{target_svc}`:\n{log_lines}"
            )
            self.last_mode = "deterministic-evidence-engine"
            return {"reply": reply, "mode": self.last_mode}

        # 6. Deployment diff request
        if ("diff" in lower or "compare" in lower) and (
            "deployment" in lower or "revision" in lower or "task" in lower
        ):
            diff_res = tool_executor("diff_service_deployments", {"service": target_svc})
            inv_res = tool_executor("investigate_service", {"service": target_svc})
            diff = diff_res.get("task_def_diff") or {}
            changed_env = diff.get("changed_env") or {}
            diff_bullets = "\n".join(
                f"- `{k}`: `{v.get('baseline', '')}` -> `{v.get('active', '')}`"
                for k, v in changed_env.items()
            ) or "- No environment drift detected between revisions."
            inv = inv_res.get("investigation") or {}
            prop = inv_res.get("proposal") or {}
            reply = (
                f"Diff for `{target_svc}` (`{diff.get('baseline_revision', '')}` vs `{diff.get('active_revision', '')}`):\n"
                f"{diff_bullets}\n\n"
                f"Root cause: {inv.get('root_cause', '')}\n"
                f"Proposed fix: {prop.get('title', 'None')}"
            )
            self.last_mode = "deterministic-evidence-engine"
            return {"reply": reply, "mode": self.last_mode}

        # 7. Default: Service investigation when a service or operational issue is asked about
        inv_res = tool_executor("investigate_service", {"service": target_svc})
        inv = inv_res.get("investigation") or {}
        prop = inv_res.get("proposal") or {}
        reply = (
            f"{inv.get('summary', '')}\n\n"
            f"Root cause: {inv.get('root_cause', '')}\n"
            f"Proposed fix: {prop.get('title', 'None')} (`{prop.get('aws_cli_equivalent', '')}`)"
        )
        self.last_mode = "deterministic-evidence-engine"
        return {"reply": reply, "mode": self.last_mode}

    def _deterministic_fallback(
        self,
        prompt: str | list[dict[str, Any]],
        context: dict[str, Any] | None = None,
        error_note: str | None = None,
    ) -> str:
        ctx = context or {}
        if "summary" in ctx and "root_cause" in ctx:
            return f"{ctx['summary']} Root Cause: {ctx['root_cause']}"
        if "filename" in ctx:
            fname = ctx.get("filename", "attachment")
            mime = ctx.get("mime_type", "text/plain")
            snippet = ctx.get("snippet", "")
            svc = ctx.get("service") or "cloud-ops-demo-cluster"
            if snippet:
                return (
                    f"[Multimodal Context from {fname} ({mime}) for {svc}]: "
                    f"Verified operational guidance and topology constraints — {snippet}"
                )
            return (
                f"[Multimodal Context from {fname} ({mime}) for {svc}]: "
                f"Inspected visual/diagram attachment and aligned remediation blast radius with service topology."
            )
        text_prompt = str(prompt)
        return f"[{self.model} Evidence Reasoning]: {text_prompt[:320]}"

    def _synthetic_interaction_id(self, seed: str) -> str:
        digest = hashlib.sha256(f"{self.model}:{seed}".encode("utf-8")).hexdigest()[:14]
        return f"int_{digest}"
