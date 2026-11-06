"""End-to-end unit and integration tests for Cloud Ops Agent."""

from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest
import zipfile

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cloud_ops.config import AgentConfig
from cloud_ops.llm.gemini_engine import GeminiReasoningEngine
from cloud_ops.orchestrator import CloudOpsOrchestrator


class TestCloudOpsAgentBackend(unittest.TestCase):
    """Comprehensive tests for Investigator, Remediator, Verifier, Guardrails, Memory, and Artifacts."""

    def setUp(self) -> None:
        os.environ["CLOUD_OPS_OFFLINE_LLM"] = "1"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.config = AgentConfig(
            gemini_model="gemini-3.8-flash",
            use_vertex_ai=True,
            vertex_project="cog01m1hm7b45680madcxrzh1adja",
            vertex_location="global",
            aws_region="us-east-1",
            ecs_cluster="cloud-ops-demo-cluster",
            live_aws=False,
            artifacts_dir=self.tmp_path / "artifacts",
            data_dir=self.tmp_path / "data",
        )
        self.orchestrator = CloudOpsOrchestrator(config=self.config)

    def tearDown(self) -> None:
        os.environ.pop("CLOUD_OPS_OFFLINE_LLM", None)
        self.temp_dir.cleanup()

    def test_01_gemini_model_enforces_3_8_flash(self) -> None:
        """Verify GeminiReasoningEngine defaults to gemini-3.8-flash + Vertex AI ADC config."""
        engine_default = GeminiReasoningEngine()
        self.assertEqual(engine_default.model, "gemini-3.8-flash")
        self.assertTrue(engine_default.use_vertex_ai)
        self.assertEqual(engine_default.vertex_project, "cog01m1hm7b45680madcxrzh1adja")
        self.assertEqual(engine_default.vertex_location, "global")

        engine_deprecated = GeminiReasoningEngine(model="gemini-2.5-flash")
        self.assertEqual(engine_deprecated.model, "gemini-3.8-flash")
        self.assertTrue(engine_deprecated.model_substituted)

        res = engine_default.reason("Diagnose HTTP 500 spike on salescreate-api")
        self.assertEqual(res["model"], "gemini-3.8-flash")
        self.assertTrue(len(res["text"]) > 10)

    def test_02_full_incident_lifecycle_salescreate_bad_release(self) -> None:
        """Test end-to-end lifecycle: bad-release -> Investigator -> Remediator -> Approval -> Verifier -> Memory."""
        # 1. Start investigation on salescreate-api (bad-release)
        incident = self.orchestrator.start_investigation(
            service="salescreate-api",
            scenario="bad-release",
            user_query="Why is salescreate-api throwing HTTP 500 errors?",
            attachments=[
                {
                    "filename": "salescreate_runbook.md",
                    "mime_type": "text/markdown",
                    "content": "# Runbook: Roll back salescreate-api to v2.4.0 if KeyError in calculate_tier_discount occurs.",
                }
            ],
        )
        self.assertEqual(incident.status, "PENDING_APPROVAL")
        self.assertIsNotNone(incident.investigation)
        self.assertIsNotNone(incident.proposal)

        inv = incident.investigation
        assert inv is not None
        # Verify real microservice metrics (bad-release has 35% error rate across 20 ticks)
        self.assertAlmostEqual(inv.metrics_snapshot["error_rate_pct"], 35.0, places=1)
        self.assertEqual(len(inv.steps_trace), 4)

        # Verify hypotheses include both RULED_OUT (course adjustment) and CONFIRMED
        statuses = {h.status for h in inv.hypotheses}
        self.assertIn("RULED_OUT", statuses)
        self.assertIn("CONFIRMED", statuses)

        # Verify task definition diff pinpoints v2.4.0 -> v2.4.1-bad
        self.assertIsNotNone(inv.task_def_diff)
        assert inv.task_def_diff is not None
        self.assertEqual(
            inv.task_def_diff.changed_env["RELEASE_VERSION"]["baseline"], "v2.4.0"
        )
        self.assertEqual(
            inv.task_def_diff.changed_env["RELEASE_VERSION"]["active"], "v2.4.1-bad"
        )

        # Verify Incident Memory matched past incident INC-2026-0928
        self.assertIsNotNone(inv.matched_past_incident)
        assert inv.matched_past_incident is not None
        self.assertEqual(inv.matched_past_incident["incident_id"], "INC-2026-0928")
        self.assertEqual(len(inv.multimodal_insights), 1)

        # Verify Remediator proposal is PENDING_APPROVAL
        prop = incident.proposal
        assert prop is not None
        self.assertEqual(prop.status, "PENDING_APPROVAL")
        self.assertTrue(prop.requires_approval)
        self.assertEqual(prop.mcp_tool, "rollback_task_definition")

        # 2. Approve remediation -> executes rollback -> runs Verifier -> saves to Memory
        resolved = self.orchestrator.approve_remediation(
            incident.incident_id, approver="sre-lead@cloudops.io"
        )
        self.assertEqual(resolved.status, "VERIFIED")
        self.assertIsNotNone(resolved.verification)
        assert resolved.verification is not None
        self.assertTrue(resolved.verification.verified)
        self.assertFalse(resolved.verification.rollback_triggered)
        self.assertLess(resolved.verification.post_metrics["error_rate_pct"], 1.0)

        # Verify persisted in memory_store.json
        saved_ids = [item["incident_id"] for item in self.orchestrator.memory.past_incidents]
        self.assertIn(incident.incident_id, saved_ids)

    def test_03_off_business_hours_scaling_with_environment_memory(self) -> None:
        """Test natural language off-business hours scaling workflow using Environment Memory."""
        chat_res = self.orchestrator.handle_chat(
            "Scale down the SalesCreate ECS Service for the off-business hours"
        )
        self.assertEqual(chat_res["intent"], "scaling_proposal")
        incident_id = chat_res["incident_id"]
        prop = chat_res["proposal"]
        self.assertEqual(prop["mcp_tool"], "scale_ecs_service")
        self.assertEqual(prop["status"], "PENDING_APPROVAL")
        self.assertEqual(prop["parameters"]["desired_count"], 1)

        # Approve scaling proposal and verify ECS desired_count updated to 1
        updated_inc = self.orchestrator.approve_remediation(incident_id, approver="finops@team")
        self.assertEqual(updated_inc.status, "VERIFIED")
        ecs_desc = self.orchestrator.ecs.describe_ecs_service("salescreate-api")
        self.assertEqual(ecs_desc["desired_count"], 1)

    def test_04_iam_inspection_and_destructive_cli_guardrails(self) -> None:
        """Verify IAM role inspection and GuardrailedAWSCLI blocking destructive & unapproved commands."""
        # 1. Inspect IAM role permissions via chat
        iam_chat = self.orchestrator.handle_chat("What can this IAM role write?")
        self.assertEqual(iam_chat["intent"], "iam_inspection")
        perms = iam_chat["iam_permissions"]
        self.assertIn("ecs:UpdateService", perms["allowlisted_write_actions"])
        self.assertIn("ecs:DeleteCluster", perms["explicitly_denied_actions"])

        # 2. Attempt destructive delete-cluster via chat and direct CLI guard
        blocked_chat = self.orchestrator.handle_chat(
            "aws ecs delete-cluster --cluster cloud-ops-demo-cluster"
        )
        self.assertEqual(blocked_chat["intent"], "guardrail_blocked")
        self.assertTrue(blocked_chat["blocked"])
        self.assertFalse(blocked_chat["validation"]["allowed"])
        self.assertEqual(blocked_chat["validation"]["category"], "DENIED_DESTRUCTIVE")

        # 3. Attempt write operation without approval token -> raises PermissionError
        with self.assertRaises(PermissionError):
            self.orchestrator.ecs.rollback_task_definition(
                service="salescreate-api",
                target_revision="salescreate-api:14",
                approval_token="",
            )

    def test_05_multimodal_artifacts_excel_svg_docx_markdown(self) -> None:
        """Verify .xlsx, .svg, .docx, and .md artifacts are generated and structurally valid OpenXML/SVG."""
        incident = self.orchestrator.start_investigation(
            service="notifier",
            scenario="secret-rotation-mismatch",
            user_query="Notifier is failing with 503 SecretAuthError",
        )
        self.orchestrator.approve_remediation(incident.incident_id)
        artifacts = self.orchestrator.generate_artifacts(incident.incident_id)
        self.assertEqual(len(artifacts), 4)

        by_kind = {a.kind: a for a in artifacts}
        self.assertIn("excel", by_kind)
        self.assertIn("chart", by_kind)
        self.assertIn("docx", by_kind)
        self.assertIn("markdown", by_kind)

        # Verify Excel (.xlsx) is a valid ZIP archive with required OpenXML parts
        xlsx_path = Path(by_kind["excel"].path)
        self.assertTrue(xlsx_path.exists())
        self.assertTrue(zipfile.is_zipfile(xlsx_path))
        with zipfile.ZipFile(xlsx_path, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("[Content_Types].xml", names)
            self.assertIn("xl/workbook.xml", names)
            self.assertIn("xl/worksheets/sheet1.xml", names)
            self.assertIn("xl/worksheets/sheet2.xml", names)
            self.assertIn("xl/worksheets/sheet3.xml", names)

        # Verify Word (.docx) is a valid ZIP archive with word/document.xml
        docx_path = Path(by_kind["docx"].path)
        self.assertTrue(docx_path.exists())
        self.assertTrue(zipfile.is_zipfile(docx_path))
        with zipfile.ZipFile(docx_path, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("[Content_Types].xml", names)
            self.assertIn("word/document.xml", names)
            doc_xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn(incident.service, doc_xml)

        # Verify SVG chart contains valid <svg> markup and phase annotations
        svg_path = Path(by_kind["chart"].path)
        self.assertTrue(svg_path.exists())
        svg_text = svg_path.read_text(encoding="utf-8")
        self.assertIn("<svg", svg_text)
        self.assertIn("Fault Injected", svg_text)
        self.assertIn("Verifier Confirmed Recovery", svg_text)

    def test_06_multi_service_scenarios_and_dashboard_state(self) -> None:
        """Verify scenario injection and investigation on inventory-worker (lock-contention)."""
        self.orchestrator.inject_scenario("inventory-worker", "lock-contention")
        inc = self.orchestrator.start_investigation(service="inventory-worker")
        assert inc.investigation is not None
        assert inc.investigation.task_def_diff is not None
        self.assertEqual(
            inc.investigation.task_def_diff.changed_env["LOCK_TIMEOUT_MS"]["active"],
            "30",
        )
        self.assertIsNotNone(inc.investigation.matched_past_incident)
        assert inc.investigation.matched_past_incident is not None
        self.assertEqual(
            inc.investigation.matched_past_incident["incident_id"], "INC-2026-1004"
        )

        state = self.orchestrator.get_dashboard_state()
        self.assertIn("salescreate-api", state["services"])
        self.assertIn("inventory-worker", state["services"])
        self.assertIn("notifier", state["services"])

    def test_07_dynamic_arbitrary_ecs_service_and_conversation_store(self) -> None:
        """Verify arbitrary external ECS service names work without ValueError and persist in ConversationStore."""
        ext_service = "checkout-payment-ecs"
        self.orchestrator.cloudwatch.set_service_scenario(ext_service, "degraded-dependency")

        conv = self.orchestrator.create_conversation(title="External Service Check")
        conv_id = conv["conversation_id"]

        res = self.orchestrator.handle_chat(
            message=f"Why is {ext_service} throwing errors and export an Excel report?",
            conversation_id=conv_id,
        )
        self.assertEqual(res["conversation_id"], conv_id)
        self.assertEqual(res["service"], ext_service)

        msg = res["message"]
        self.assertEqual(msg["role"], "assistant")
        self.assertGreater(len(msg["executed_commands"]), 0)
        self.assertTrue(
            all("command" in cmd and "tool_name" in cmd for cmd in msg["executed_commands"])
        )

        # Verify persisted on disk in conversations.json
        loaded_conv = self.orchestrator.get_conversation(conv_id)
        self.assertIsNotNone(loaded_conv)
        assert loaded_conv is not None
        self.assertEqual(loaded_conv["active_service"], ext_service)
        self.assertEqual(len(loaded_conv["messages"]), 2)
        self.assertGreater(len(loaded_conv["messages"][1]["executed_commands"]), 0)


if __name__ == "__main__":
    unittest.main()
