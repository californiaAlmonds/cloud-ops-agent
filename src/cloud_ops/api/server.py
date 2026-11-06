"""Platform-agnostic HTTP API + Web Console server using Python's standard library."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from cloud_ops.config import AgentConfig
from cloud_ops.mcp_servers.cloudwatch_mcp import SUPPORTED_SERVICES
from cloud_ops.orchestrator import CloudOpsOrchestrator

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

STATIC_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

ARTIFACT_MIME_TYPES: dict[str, str] = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".svg": "image/svg+xml; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}

_INCIDENT_ACTION_RE = re.compile(
    r"^/api/incidents/(?P<incident_id>[^/]+)/(?P<action>approve|reject|artifacts)/?$"
)
_CONVERSATION_ITEM_RE = re.compile(r"^/api/conversations/(?P<conversation_id>[^/]+)/?$")
_CONVERSATION_MESSAGES_RE = re.compile(
    r"^/api/conversations/(?P<conversation_id>[^/]+)/messages/?$"
)

_DEFAULT_ORCHESTRATOR: CloudOpsOrchestrator | None = None


def get_default_orchestrator() -> CloudOpsOrchestrator:
    """Return a shared module-level orchestrator singleton for serverless runtimes (e.g. Vercel)."""
    global _DEFAULT_ORCHESTRATOR
    if _DEFAULT_ORCHESTRATOR is None:
        _DEFAULT_ORCHESTRATOR = CloudOpsOrchestrator(AgentConfig.from_env())
    return _DEFAULT_ORCHESTRATOR


class CloudOpsHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer bound to a shared CloudOpsOrchestrator instance."""

    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        orchestrator: CloudOpsOrchestrator | None = None,
    ) -> None:
        self.orchestrator = orchestrator or CloudOpsOrchestrator(AgentConfig.from_env())
        super().__init__(server_address, CloudOpsRequestHandler)


class CloudOpsRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving the Web Console UI, REST API, Artifacts, and ChatOps Webhooks."""

    server: CloudOpsHTTPServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Keep console output clean during automated tests while logging errors."""
        return

    @property
    def orchestrator(self) -> CloudOpsOrchestrator:
        srv_orch = getattr(self.server, "orchestrator", None)
        return srv_orch if srv_orch is not None else get_default_orchestrator()

    # -------------------------------------------------------------------------
    # CORS & Response Helpers
    # -------------------------------------------------------------------------

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
        content_disposition: str | None = None,
    ) -> None:
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        if content_disposition:
            self.send_header("Content-Disposition", content_disposition)
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            # Support URL-encoded Slack slash command payloads as well
            qs = parse_qs(raw)
            return {k: v[0] for k, v in qs.items() if v}

    # -------------------------------------------------------------------------
    # HTTP Methods
    # -------------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                self._serve_ui_file("index.html")
                return

            if path in ("/styles.css", "/app.js"):
                self._serve_ui_file(path.lstrip("/"))
                return

            if path.startswith("/static/"):
                rel_name = path[len("/static/") :]
                self._serve_ui_file(rel_name)
                return

            if path == "/api/health":
                self._send_json(
                    {
                        "status": "ok",
                        "model": self.orchestrator.config.gemini_model,
                        "llm_status": self.orchestrator.gemini.status_summary(),
                        "cluster": self.orchestrator.config.ecs_cluster,
                        "region": self.orchestrator.config.aws_region,
                        "live_aws": self.orchestrator.config.live_aws,
                        "services": list(SUPPORTED_SERVICES),
                    }
                )
                return

            if path == "/api/state":
                self._send_json(self.orchestrator.get_dashboard_state())
                return

            if path in ("/api/conversations", "/api/conversations/"):
                self._send_json(self.orchestrator.list_conversations())
                return

            conv_match = _CONVERSATION_ITEM_RE.match(path)
            if conv_match:
                conv_id = unquote(conv_match.group("conversation_id"))
                conv = self.orchestrator.get_conversation(conv_id)
                if conv is None:
                    self._send_json(
                        {"error": f"Conversation not found: {conv_id}"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self._send_json({"conversation": conv})
                return

            if path.startswith("/artifacts/") or path.startswith("/api/artifacts/"):
                prefix = "/api/artifacts/" if path.startswith("/api/artifacts/") else "/artifacts/"
                filename = unquote(path[len(prefix) :])
                force_download = query.get("download", ["0"])[0] in ("1", "true", "yes")
                self._serve_artifact_file(filename, force_download=force_download)
                return

            self._send_json(
                {"error": f"Route not found: {path}"},
                status=HTTPStatus.NOT_FOUND,
            )
        except Exception as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        try:
            if path in ("/api/conversations", "/api/conversations/"):
                title = body.get("title")
                if title is not None:
                    title = str(title).strip() or None
                conv = self.orchestrator.create_conversation(title=title)
                self._send_json(
                    {
                        "conversation": conv,
                        **self.orchestrator.list_conversations(),
                    }
                )
                return

            msg_match = _CONVERSATION_MESSAGES_RE.match(path)
            if msg_match:
                conv_id = unquote(msg_match.group("conversation_id"))
                message = str(body.get("message") or body.get("query") or "")
                attachments = body.get("attachments")
                if not isinstance(attachments, list):
                    attachments = None
                chat_res = self.orchestrator.handle_chat(
                    message=message,
                    attachments=attachments,
                    conversation_id=conv_id,
                )
                chat_res["state"] = self.orchestrator.get_dashboard_state()
                self._send_json(chat_res)
                return

            if path == "/api/chat":
                message = str(body.get("message") or body.get("query") or "")
                attachments = body.get("attachments")
                if not isinstance(attachments, list):
                    attachments = None
                conversation_id = body.get("conversation_id")
                if conversation_id is not None:
                    conversation_id = str(conversation_id).strip() or None
                chat_res = self.orchestrator.handle_chat(
                    message=message,
                    attachments=attachments,
                    conversation_id=conversation_id,
                )
                chat_res["state"] = self.orchestrator.get_dashboard_state()
                self._send_json(chat_res)
                return

            if path == "/api/investigate":
                service = str(body.get("service") or "salescreate-api")
                scenario = body.get("scenario")
                if scenario is not None:
                    scenario = str(scenario).strip() or None
                query = str(body.get("query") or body.get("message") or "")
                attachments = body.get("attachments")
                if not isinstance(attachments, list):
                    attachments = None
                incident = self.orchestrator.start_investigation(
                    service=service,
                    scenario=scenario,
                    user_query=query,
                    attachments=attachments,
                )
                self._send_json(
                    {
                        "incident_id": incident.incident_id,
                        "incident": incident.to_dict(),
                        "investigation": (
                            incident.investigation.to_dict() if incident.investigation else None
                        ),
                        "proposal": incident.proposal.to_dict() if incident.proposal else None,
                        "state": self.orchestrator.get_dashboard_state(),
                    }
                )
                return

            if path == "/api/scenarios/inject":
                service = str(body.get("service") or "salescreate-api")
                scenario = str(body.get("scenario") or "healthy")
                inject_res = self.orchestrator.inject_scenario(service=service, scenario=scenario)
                inject_res["state"] = self.orchestrator.get_dashboard_state()
                self._send_json(inject_res)
                return

            match = _INCIDENT_ACTION_RE.match(path)
            if match:
                incident_id = unquote(match.group("incident_id"))
                action = match.group("action")
                conversation_id = body.get("conversation_id")
                if conversation_id is not None:
                    conversation_id = str(conversation_id).strip() or None

                if action == "approve":
                    approver = str(body.get("approver") or "sre-oncall@cloudops.io")
                    incident = self.orchestrator.approve_remediation(
                        incident_id=incident_id,
                        approver=approver,
                        conversation_id=conversation_id,
                    )
                    target_conv_id = (
                        conversation_id
                        or self.orchestrator.conversation_store.active_conversation_id
                        or ""
                    )
                    self._send_json(
                        {
                            "incident_id": incident.incident_id,
                            "incident": incident.to_dict(),
                            "verification": (
                                incident.verification.to_dict() if incident.verification else None
                            ),
                            "conversation": self.orchestrator.get_conversation(target_conv_id),
                            "state": self.orchestrator.get_dashboard_state(),
                        }
                    )
                    return

                if action == "reject":
                    reason = str(body.get("reason") or "Rejected by operator via Web Console")
                    incident = self.orchestrator.reject_remediation(
                        incident_id=incident_id,
                        reason=reason,
                        conversation_id=conversation_id,
                    )
                    target_conv_id = (
                        conversation_id
                        or self.orchestrator.conversation_store.active_conversation_id
                        or ""
                    )
                    self._send_json(
                        {
                            "incident_id": incident.incident_id,
                            "incident": incident.to_dict(),
                            "conversation": self.orchestrator.get_conversation(target_conv_id),
                            "state": self.orchestrator.get_dashboard_state(),
                        }
                    )
                    return

                if action == "artifacts":
                    records = self.orchestrator.generate_artifacts(incident_id=incident_id)
                    incident = self.orchestrator.incidents[incident_id]
                    artifacts_payload = []
                    for rec in records:
                        d = rec.to_dict()
                        d["download_url"] = f"/artifacts/{rec.name}?download=1"
                        d["preview_url"] = f"/artifacts/{rec.name}"
                        artifacts_payload.append(d)
                    self._send_json(
                        {
                            "incident_id": incident_id,
                            "artifacts": artifacts_payload,
                            "incident": incident.to_dict(),
                            "state": self.orchestrator.get_dashboard_state(),
                        }
                    )
                    return

            if path == "/api/webhooks/slack":
                self._handle_slack_webhook(body)
                return

            if path == "/api/webhooks/teams":
                self._handle_teams_webhook(body)
                return

            self._send_json(
                {"error": f"Unknown POST endpoint: {path}"},
                status=HTTPStatus.NOT_FOUND,
            )
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except (ValueError, PermissionError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            conv_match = _CONVERSATION_ITEM_RE.match(path)
            if conv_match:
                conv_id = unquote(conv_match.group("conversation_id"))
                deleted = self.orchestrator.delete_conversation(conv_id)
                if not deleted:
                    self._send_json(
                        {
                            "error": f"Conversation not found: {conv_id}",
                            "deleted": False,
                        },
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self._send_json(
                    {
                        "deleted": True,
                        "conversation_id": conv_id,
                        **self.orchestrator.list_conversations(),
                    }
                )
                return

            self._send_json(
                {"error": f"Unknown DELETE endpoint: {path}"},
                status=HTTPStatus.NOT_FOUND,
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    # -------------------------------------------------------------------------
    # Static UI & Artifact File Serving
    # -------------------------------------------------------------------------

    def _serve_ui_file(self, relative_name: str) -> None:
        safe_name = Path(relative_name).name
        target_path = (UI_DIR / safe_name).resolve()
        if not target_path.exists() or not target_path.is_file():
            self._send_json(
                {"error": f"Static UI asset not found: {safe_name}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        ext = target_path.suffix.lower()
        content_type = STATIC_MIME_TYPES.get(ext, "application/octet-stream")
        self._send_bytes(target_path.read_bytes(), content_type=content_type)

    def _serve_artifact_file(self, filename: str, force_download: bool = False) -> None:
        safe_name = Path(filename).name
        artifacts_dir = self.orchestrator.config.artifacts_dir.resolve()
        target_path = (artifacts_dir / safe_name).resolve()

        if not str(target_path).startswith(str(artifacts_dir)) or not target_path.exists():
            self._send_json(
                {"error": f"Artifact not found: {safe_name}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        ext = target_path.suffix.lower()
        content_type = ARTIFACT_MIME_TYPES.get(ext, "application/octet-stream")
        if ext in (".xlsx", ".docx") or force_download:
            disposition = f'attachment; filename="{safe_name}"'
        else:
            disposition = f'inline; filename="{safe_name}"'

        self._send_bytes(
            target_path.read_bytes(),
            content_type=content_type,
            content_disposition=disposition,
        )

    # -------------------------------------------------------------------------
    # Platform-Agnostic ChatOps Webhook Adapters (Slack Block Kit & Teams Adaptive Card)
    # -------------------------------------------------------------------------

    def _handle_slack_webhook(self, body: dict[str, Any]) -> None:
        text = str(body.get("text") or body.get("command_text") or "Why is salescreate-api failing?")
        user_name = str(body.get("user_name") or body.get("user") or "slack-operator")
        chat_res = self.orchestrator.handle_chat(message=text)
        reply_text = str(chat_res.get("reply") or "")
        incident_id = chat_res.get("incident_id") or self.orchestrator.active_incident_id or "N/A"
        proposal = chat_res.get("proposal") or {}

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Cloud Ops Agent ({self.orchestrator.config.gemini_model})",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Operator:* @{user_name}\n*Query:* `{text}`\n\n{reply_text}",
                },
            },
        ]
        if proposal:
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Service:*\n`{chat_res.get('service') or 'ecs'}`"},
                        {"type": "mrkdwn", "text": f"*Approval Status:*\n`{proposal.get('status')}`"},
                        {"type": "mrkdwn", "text": f"*MCP Tool:*\n`{proposal.get('mcp_tool')}`"},
                        {"type": "mrkdwn", "text": f"*CLI Command:*\n`{proposal.get('aws_cli_equivalent')}`"},
                    ],
                }
            )

        self._send_json(
            {
                "response_type": "in_channel",
                "platform": "slack",
                "user_name": user_name,
                "incident_id": incident_id,
                "intent": chat_res.get("intent"),
                "text": reply_text,
                "blocks": blocks,
            }
        )

    def _handle_teams_webhook(self, body: dict[str, Any]) -> None:
        text = str(body.get("text") or body.get("message") or "Why is salescreate-api failing?")
        user_name = str(body.get("user_name") or body.get("from") or "teams-operator")
        chat_res = self.orchestrator.handle_chat(message=text)
        reply_text = str(chat_res.get("reply") or "")
        incident_id = chat_res.get("incident_id") or self.orchestrator.active_incident_id or "N/A"
        proposal = chat_res.get("proposal") or {}

        facts = [
            {"title": "Operator", "value": user_name},
            {"title": "Model", "value": self.orchestrator.config.gemini_model},
            {"title": "Service", "value": str(chat_res.get("service") or "ecs")},
            {"title": "Intent", "value": str(chat_res.get("intent", "investigate"))},
        ]
        if proposal:
            facts.append({"title": "Proposed Tool", "value": str(proposal.get("mcp_tool", ""))})
            facts.append({"title": "Approval Gate", "value": str(proposal.get("status", "PENDING_APPROVAL"))})

        adaptive_card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {
                    "type": "TextBlock",
                    "size": "Large",
                    "weight": "Bolder",
                    "text": "Cloud Ops Agent — Operational Intelligence",
                },
                {
                    "type": "TextBlock",
                    "wrap": True,
                    "text": reply_text,
                },
                {
                    "type": "FactSet",
                    "facts": facts,
                },
            ],
        }

        self._send_json(
            {
                "type": "message",
                "platform": "teams",
                "user_name": user_name,
                "incident_id": incident_id,
                "intent": chat_res.get("intent"),
                "text": reply_text,
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": adaptive_card,
                    }
                ],
            }
        )


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    orchestrator: CloudOpsOrchestrator | None = None,
) -> CloudOpsHTTPServer:
    """Create a configured CloudOpsHTTPServer instance (useful for tests or custom embedding)."""
    return CloudOpsHTTPServer((host, port), orchestrator=orchestrator)


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    orchestrator: CloudOpsOrchestrator | None = None,
) -> None:
    """Start the Cloud Ops HTTP API + Web Console server and block until interrupted."""
    server = create_server(host=host, port=port, orchestrator=orchestrator)
    actual_host, actual_port = server.server_address[:2]
    display_host = "localhost" if actual_host in ("0.0.0.0", "127.0.0.1") else actual_host
    llm_info = server.orchestrator.gemini.status_summary()
    print(f"Cloud Ops Agent Mission Control listening on http://{display_host}:{actual_port}")
    print(
        f"  Model      : {server.orchestrator.config.gemini_model} "
        f"(Auth: {llm_info['auth_mode']}, Project: {llm_info['vertex_project']}, Location: {llm_info['vertex_location']})"
    )
    print(
        f"  AWS Mode   : {'LIVE AWS (' + server.orchestrator.config.aws_region + ')' if server.orchestrator.config.live_aws else 'LOCAL SIMULATION (set CLOUD_OPS_LIVE_AWS=true for live AWS)'}"
    )
    print(f"  Cluster    : {server.orchestrator.config.ecs_cluster} ({server.orchestrator.config.aws_region})")
    print(f"  Artifacts  : {server.orchestrator.config.artifacts_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Cloud Ops Agent server...")
    finally:
        server.server_close()
