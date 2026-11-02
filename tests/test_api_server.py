"""End-to-end HTTP API & Web Console unit tests for Cloud Ops Agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.index import handler as VercelHandler  # noqa: E402
from cloud_ops.api.server import CloudOpsRequestHandler, create_server  # noqa: E402
from cloud_ops.config import AgentConfig  # noqa: E402
from cloud_ops.orchestrator import CloudOpsOrchestrator  # noqa: E402


class TestCloudOpsAPIServer(unittest.TestCase):
    """Verify the single-repo HTTP server serves both Web Console UI and API endpoints."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["CLOUD_OPS_OFFLINE_LLM"] = "1"
        root = Path(__file__).resolve().parent.parent
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(cls._tmpdir.name)
        data_dir = tmp_path / "data"
        artifacts_dir = tmp_path / "artifacts"
        shutil.copytree(root / "data", data_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.config = AgentConfig(
            artifacts_dir=artifacts_dir,
            data_dir=data_dir,
        )
        cls.orchestrator = CloudOpsOrchestrator(config=cls.config)
        cls.server = create_server(host="127.0.0.1", port=0, orchestrator=cls.orchestrator)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.pop("CLOUD_OPS_OFFLINE_LLM", None)
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmpdir.cleanup()

    def _get(self, path: str) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=30) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _delete_json(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_01_ui_and_static_assets_served(self) -> None:
        status, html_bytes, ctype = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        html_text = html_bytes.decode("utf-8")
        self.assertIn('closedby="any"', html_text)
        self.assertIn("Cloud Ops Agent", html_text)

        js_status, js_bytes, _ = self._get("/static/app.js")
        self.assertEqual(js_status, 200)
        self.assertIn("closedBy", js_bytes.decode("utf-8"))

    def test_02_api_health_and_state(self) -> None:
        status, body, _ = self._get("/api/health")
        self.assertEqual(status, 200)
        health = json.loads(body.decode("utf-8"))
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["model"], "gemini-3.8-flash")

        s_status, s_body, _ = self._get("/api/state")
        self.assertEqual(s_status, 200)
        state = json.loads(s_body.decode("utf-8"))
        self.assertIn("services", state)
        self.assertIn("salescreate-api", state["services"])

    def test_03_api_investigate_approve_and_artifacts(self) -> None:
        status, inv = self._post_json(
            "/api/investigate",
            {
                "service": "salescreate-api",
                "scenario": "bad-release",
                "query": "Why is salescreate-api throwing HTTP 500 errors?",
            },
        )
        self.assertEqual(status, 200)
        incident = inv.get("incident", inv)
        inc_id = incident["incident_id"]
        self.assertEqual(incident["status"], "PENDING_APPROVAL")

        app_status, app_resp = self._post_json(
            f"/api/incidents/{inc_id}/approve",
            {"approver": "test-operator@cloudops.io"},
        )
        self.assertEqual(app_status, 200)
        approved_inc = app_resp.get("incident", app_resp)
        self.assertEqual(approved_inc["status"], "VERIFIED")

        art_status, art_resp = self._post_json(f"/api/incidents/{inc_id}/artifacts", {})
        self.assertEqual(art_status, 200)
        artifacts = art_resp.get("artifacts", [])
        self.assertGreaterEqual(len(artifacts), 4)

        # Verify downloading the generated Excel file works
        xlsx_url = artifacts[0]["url"]
        dl_status, dl_bytes, _ = self._get(xlsx_url)
        self.assertEqual(dl_status, 200)
        self.assertTrue(dl_bytes.startswith(b"PK"))

    def test_04_slack_and_teams_webhooks(self) -> None:
        s_status, slack_resp = self._post_json(
            "/api/webhooks/slack",
            {"text": "What can the IAM role write?", "user_name": "sre_lead"},
        )
        self.assertEqual(s_status, 200)
        self.assertIn("blocks", slack_resp)

        t_status, teams_resp = self._post_json(
            "/api/webhooks/teams",
            {"text": "Scale down SalesCreate for off-business hours", "user_name": "sre_lead"},
        )
        self.assertEqual(t_status, 200)
        self.assertEqual(teams_resp.get("type"), "message")

    def test_05_vercel_single_package_adapter(self) -> None:
        """Verify Vercel Python serverless handler and VERCEL=1 /tmp storage config."""
        self.assertTrue(issubclass(VercelHandler, CloudOpsRequestHandler))
        prev = os.environ.get("VERCEL")
        try:
            os.environ["VERCEL"] = "1"
            vercel_cfg = AgentConfig.from_env()
            self.assertIn("cloud-ops-agent", str(vercel_cfg.artifacts_dir))
            self.assertTrue((vercel_cfg.data_dir / "memory_store.json").exists())
            self.assertTrue((vercel_cfg.data_dir / "conversations.json").exists())
        finally:
            if prev is None:
                os.environ.pop("VERCEL", None)
            else:
                os.environ["VERCEL"] = prev

    def test_06_conversations_crud_and_inline_approval_flow(self) -> None:
        """Test /api/conversations CRUD, per-turn executed_commands, and inline approval with conversation_id."""
        # Ensure salescreate-api is in bad-release scenario for diagnostic + approval test
        self._post_json(
            "/api/scenarios/inject",
            {"service": "salescreate-api", "scenario": "bad-release"},
        )

        # 1. Create a new conversation
        c_status, created = self._post_json(
            "/api/conversations", {"title": "Checkout 503 Investigation"}
        )
        self.assertIn(c_status, (200, 201))
        conv = created["conversation"]
        conv_id = conv["conversation_id"]

        # 2. Send a diagnostic prompt to /api/conversations/<conv_id>/messages
        m_status, chat_res = self._post_json(
            f"/api/conversations/{conv_id}/messages",
            {"message": "Why is salescreate-api failing? Show me a chart and fix it."},
        )
        self.assertEqual(m_status, 200)
        self.assertEqual(chat_res["conversation_id"], conv_id)
        assistant_msg = chat_res["message"]
        self.assertGreater(len(assistant_msg["executed_commands"]), 0)
        self.assertIsNotNone(assistant_msg["proposal"])
        self.assertEqual(assistant_msg["proposal"]["status"], "PENDING_APPROVAL")
        inc_id = assistant_msg["incident_id"]
        self.assertIsNotNone(inc_id)

        # 3. Approve inline with conversation_id
        a_status, app_res = self._post_json(
            f"/api/incidents/{inc_id}/approve",
            {"approver": "sre-chat-user@cloudops.io", "conversation_id": conv_id},
        )
        self.assertEqual(a_status, 200)
        self.assertEqual(app_res["incident"]["status"], "VERIFIED")
        updated_conv = app_res["conversation"]
        self.assertGreaterEqual(len(updated_conv["messages"]), 3)

        # 4. List conversations and delete the test conversation
        l_status, list_bytes, _ = self._get("/api/conversations")
        self.assertEqual(l_status, 200)
        list_payload = json.loads(list_bytes.decode("utf-8"))
        conv_ids = [c["conversation_id"] for c in list_payload["conversations"]]
        self.assertIn(conv_id, conv_ids)

        d_status, del_res = self._delete_json(f"/api/conversations/{conv_id}")
        self.assertEqual(d_status, 200)
        self.assertTrue(del_res["deleted"])

    def test_07_ui_sidebar_collapse_expand_contract(self) -> None:
        """Verify UI HTML, CSS, and JS include desktop & mobile sidebar collapse/expand controls."""
        status_html, html_bytes, _ = self._get("/")
        self.assertEqual(status_html, 200)
        html_text = html_bytes.decode("utf-8")
        self.assertIn('id="appLayout"', html_text)
        self.assertIn('id="btnCloseSidebar"', html_text)
        self.assertIn('id="btnToggleSidebar"', html_text)
        self.assertIn('class="toggle-label"', html_text)

        status_css, css_bytes, _ = self._get("/static/styles.css")
        self.assertEqual(status_css, 200)
        css_text = css_bytes.decode("utf-8")
        self.assertIn(".app-layout.sidebar-collapsed", css_text)
        self.assertIn(".app-layout.sidebar-collapsed .chat-sidebar", css_text)
        self.assertIn(".app-layout.sidebar-collapsed .sidebar-toggle-btn", css_text)

        status_js, js_bytes, _ = self._get("/static/app.js")
        self.assertEqual(status_js, 200)
        js_text = js_bytes.decode("utf-8")
        self.assertIn("function toggleSidebar()", js_text)
        self.assertIn("function collapseSidebar()", js_text)
        self.assertIn("function expandSidebar()", js_text)

    def test_08_on_demand_custom_cloudwatch_log_report_and_no_forced_artifacts(self) -> None:
        """Verify agent only creates what the user asks for and generates custom log reports dynamically."""
        # 0. Send a conversational greeting ("hi") on a fresh conversation: no tools, no investigation, no template
        _, new_conv = self._post_json("/api/conversations", {"title": "Greeting Test"})
        hi_conv_id = new_conv["conversation"]["conversation_id"]
        status_hi, hi_res = self._post_json(
            "/api/chat",
            {"message": "hi", "conversation_id": hi_conv_id},
        )
        self.assertEqual(status_hi, 200)
        self.assertEqual(hi_res["message"]["executed_commands"], [])
        self.assertIsNone(hi_res["proposal"])
        self.assertEqual(hi_res["artifacts"], [])
        self.assertIsNone(hi_res["service"])
        self.assertNotIn("Investigation Complete", hi_res["reply"])

        # 1. Inject bad-release on arbitrary service na-service-api so it has failure logs in CloudWatch
        self.orchestrator.inject_scenario("na-service-api", "bad-release")

        # 2. Ask: "create a report based on cloudwatch logs of failures of na-service-api"
        status_rep, rep_res = self._post_json(
            "/api/chat",
            {"message": "create a report based on cloudwatch logs of failures of na-service-api"},
        )
        self.assertEqual(status_rep, 200)
        self.assertEqual(rep_res["service"], "na-service-api")
        self.assertIn("na-service-api", rep_res["reply"])
        self.assertIn("CloudWatch Failure Log Analysis", rep_res["reply"])
        # Should NOT force a remediation proposal when user only asked for a log report
        self.assertIsNone(rep_res["proposal"])
        # Should ONLY generate the report document (1 docx), NOT an unsolicited SVG chart or Excel timeline
        artifacts = rep_res["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0]["name"].endswith(".docx"))

        # 3. Ask a standard diagnostic question WITHOUT asking for a chart or report
        status_diag, diag_res = self._post_json(
            "/api/chat",
            {"message": "Why is salescreate-api failing?"},
        )
        self.assertEqual(status_diag, 200)
        self.assertIsNotNone(diag_res["proposal"])
        # No unsolicited chart or report should be attached when not requested
        self.assertEqual(len(diag_res["artifacts"]), 0)

        # 4. Approve the remediation and verify no unsolicited chart/report is attached on approval
        inc_id = diag_res["incident_id"]
        conv_id = diag_res["conversation_id"]
        status_app, app_res = self._post_json(
            f"/api/incidents/{inc_id}/approve",
            {"approver": "ops-engineer@cloudops.io", "conversation_id": conv_id},
        )
        self.assertEqual(status_app, 200)
        self.assertEqual(app_res["incident"]["status"], "VERIFIED")
        last_msg = app_res["conversation"]["messages"][-1]
        self.assertEqual(len(last_msg["artifacts"]), 0)


if __name__ == "__main__":
    unittest.main()
