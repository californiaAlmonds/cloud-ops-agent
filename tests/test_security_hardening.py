"""Security and correctness regression tests for the guardrails, approvals, verifier and HTTP API."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cloud_ops.agents.verifier import VerifierAgent  # noqa: E402
from cloud_ops.api.server import create_server  # noqa: E402
from cloud_ops.approvals import ApprovalLedger  # noqa: E402
from cloud_ops.config import AgentConfig  # noqa: E402
from cloud_ops.mcp_servers.aws_cli_guard import GuardrailedAWSCLI  # noqa: E402
from cloud_ops.mcp_servers.ecs_mcp import RemediationExecutionError  # noqa: E402
from cloud_ops.orchestrator import CloudOpsOrchestrator, ProposalStateError  # noqa: E402

SERVICES = ("salescreate-api", "inventory-worker", "notifier")
UPDATE = "aws ecs update-service --cluster demo --service notifier --desired-count 0 --region us-east-1"


def make_guard(with_ledger: bool = True) -> tuple[GuardrailedAWSCLI, ApprovalLedger]:
    ledger = ApprovalLedger()
    guard = GuardrailedAWSCLI(
        allowed_cluster="demo",
        allowed_services=SERVICES,
        approval_ledger=ledger if with_ledger else None,
    )
    return guard, ledger


class TestApprovalLedger(unittest.TestCase):
    def test_token_is_single_use(self) -> None:
        ledger = ApprovalLedger()
        token = ledger.issue("PROP-1", "alice")
        self.assertTrue(ledger.is_valid(token))
        self.assertTrue(ledger.consume(token))
        self.assertFalse(ledger.consume(token))
        self.assertFalse(ledger.is_valid(token))

    def test_token_expires(self) -> None:
        now = [0.0]
        ledger = ApprovalLedger(ttl_seconds=60, clock=lambda: now[0])
        token = ledger.issue("PROP-1", "alice")
        now[0] = 61.0
        self.assertFalse(ledger.is_valid(token))
        self.assertFalse(ledger.consume(token))

    def test_multi_use_token_and_unknown_tokens(self) -> None:
        ledger = ApprovalLedger()
        token = ledger.issue("PROP-1", "alice", uses=2)
        self.assertTrue(ledger.consume(token))
        self.assertTrue(ledger.consume(token))
        self.assertFalse(ledger.consume(token))
        for bogus in (None, "", "x", "appr-deadbeef"):
            self.assertFalse(ledger.is_valid(bogus))
            self.assertFalse(ledger.consume(bogus))


class TestGuardrailedCLI(unittest.TestCase):
    def test_legitimate_reads_are_allowed(self) -> None:
        guard, _ = make_guard()
        for cmd in (
            "aws ecs describe-services --cluster demo --services notifier --region us-east-1",
            "aws logs filter-log-events --log-group-name /ecs/x --limit 20 --region us-east-1",
            "aws ecs list-task-definitions --family-prefix notifier --sort DESC --region us-east-1",
            "aws iam list-attached-role-policies --role-name cloud-ops-agent-role",
            "aws cloudwatch get-metric-statistics --namespace AWS/ECS --metric-name CPUUtilization --period 60",
            "aws sts get-caller-identity",
            "aws rds describe-db-instances",
        ):
            with self.subTest(cmd=cmd):
                res = guard.validate_command(cmd)
                self.assertTrue(res["allowed"], res["reason"])
                self.assertEqual(res["category"], "READ_ONLY")

    def test_bypass_attempts_are_blocked(self) -> None:
        guard, _ = make_guard()
        for cmd in (
            "curl http://example.com/payload get-x",
            'python -c "print(1)" list-x',
            'aws ecs stop-task --cluster demo --task abc --reason "x get-y"',
            "aws ec2 terminate-instances --instance-ids i-0abc",
            'aws ecs "delete-"service --cluster demo --service list-x',
            "aws secretsmanager get-secret-value --secret-id prod/db",
            "aws ssm get-parameter --name /x --with-decryption",
            "aws ecs describe-services --cluster demo --endpoint-url http://evil.example",
            "aws ecs describe-services --cluster demo --endpoint http://127.0.0.1:9",
            "aws ecs describe-services --cluster demo --profile other",
            "aws logs filter-log-events --log-group-name file:///etc/passwd",
            "aws ecs describe-services --cluster demo && aws ecs delete-service",
            "aws iam get-credential-report",
            "aws lambda get-function --function-name list-orders",
            "aws s3api get-object --bucket b --key k out.txt",
            "",
            "aws",
            "aws ecs",
            'aws ecs describe-services "unterminated',
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.validate_command(cmd)["allowed"])

    def test_destructive_operations_are_categorised(self) -> None:
        guard, _ = make_guard()
        for cmd in (
            "aws ecs delete-cluster --cluster demo",
            "aws ecs delete-service --cluster demo --service notifier --force",
            "aws ecs deregister-task-definition --task-definition notifier:1",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.validate_command(cmd)["category"], "DENIED_DESTRUCTIVE")

    def test_write_requires_a_real_token(self) -> None:
        guard, ledger = make_guard()
        self.assertEqual(guard.validate_command(UPDATE)["category"], "WRITE_REQUIRES_APPROVAL")
        self.assertFalse(guard.validate_command(UPDATE, approval_token="x")["allowed"])
        self.assertFalse(guard.validate_command(UPDATE, approval_token="appr-forged")["allowed"])
        no_ledger, _ = make_guard(with_ledger=False)
        self.assertFalse(no_ledger.validate_command(UPDATE, approval_token=ledger.issue("P", "a"))["allowed"])

    def test_token_is_spent_by_execution(self) -> None:
        guard, ledger = make_guard()
        token = ledger.issue("PROP-1", "alice")
        first = guard.execute_command(UPDATE, approval_token=token)
        self.assertTrue(first["allowed"])
        self.assertEqual(first["returncode"], 0)
        second = guard.execute_command(UPDATE, approval_token=token)
        self.assertFalse(second["allowed"])
        self.assertFalse(second["executed"])

    def test_writes_are_scoped_to_the_demo_cluster_and_services(self) -> None:
        guard, ledger = make_guard()
        token = ledger.issue("PROP-1", "alice")
        for cmd in (
            "aws ecs update-service --cluster other-prod --service notifier --desired-count 0",
            "aws ecs update-service --cluster demo --service payments --desired-count 0",
            "aws ecs update-service --cluster demo --service notifier --task-definition payments:3",
            "aws ecs update-service --cluster demo --service notifier --task-definition notifier:1 --task-definition payments:2",
            "aws ecs update-service --cluster demo --service notifier --cluster other-prod",
            "aws ecs update-service --cluster demo --service notifier --desired-count 11",
            "aws ecs update-service --cluster demo --service notifier --network-configuration x",
            "aws ecs update-service --cluster demo --service notifier --force-new-deployment extra",
            "aws ecs register-task-definition --family payments",
        ):
            with self.subTest(cmd=cmd):
                res = guard.validate_command(cmd, approval_token=token)
                self.assertFalse(res["allowed"])
                self.assertEqual(res["category"], "WRITE_OUT_OF_SCOPE")
        self.assertTrue(ledger.is_valid(token), "a rejected command must not spend the token")

    def test_live_mode_only_runs_the_validated_argv(self) -> None:
        guard, _ = make_guard()
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch("cloud_ops.mcp_servers.aws_cli_guard.subprocess.run", return_value=completed) as run:
            blocked = guard.execute_command('curl http://example.com get-x', live_aws=True)
            self.assertFalse(blocked["executed"])
            run.assert_not_called()

            ok = guard.execute_command(
                "aws ecs describe-services --cluster demo --services notifier", live_aws=True
            )
            self.assertTrue(ok["executed"])
            args, kwargs = run.call_args
            self.assertEqual(args[0][:3], ["aws", "ecs", "describe-services"])
            self.assertFalse(kwargs.get("shell", False))


class _OrchestratorCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["CLOUD_OPS_OFFLINE_LLM"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.orch = CloudOpsOrchestrator(
            config=AgentConfig(
                gemini_model="gemini-3.8-flash",
                use_vertex_ai=True,
                vertex_project="test-project",
                vertex_location="global",
                aws_region="us-east-1",
                ecs_cluster="cloud-ops-demo-cluster",
                live_aws=False,
                artifacts_dir=tmp / "artifacts",
                data_dir=tmp / "data",
            )
        )

    def tearDown(self) -> None:
        os.environ.pop("CLOUD_OPS_OFFLINE_LLM", None)
        self._tmp.cleanup()

    def _writes(self) -> int:
        return sum(
            1 for e in self.orch.cli_guard.audit_log if e["category"] == "ALLOWLISTED_WRITE" and e["allowed"]
        )

    def _incident(self, service: str = "salescreate-api", scenario: str = "bad-release"):
        return self.orch.start_investigation(service=service, scenario=scenario, user_query="why errors")


class TestApprovalStateMachine(_OrchestratorCase):
    def test_approved_proposal_cannot_be_approved_again(self) -> None:
        inc = self._incident()
        self.orch.approve_remediation(inc.incident_id, approver="alice")
        self.assertEqual(self._writes(), 1)
        with self.assertRaises(ProposalStateError):
            self.orch.approve_remediation(inc.incident_id, approver="alice")
        self.assertEqual(self._writes(), 1)

    def test_rejected_proposal_cannot_be_approved(self) -> None:
        inc = self._incident()
        self.orch.reject_remediation(inc.incident_id, reason="no", rejected_by="bob")
        self.assertEqual(inc.audit_trail[-1]["actor"], "bob")
        with self.assertRaises(ProposalStateError):
            self.orch.approve_remediation(inc.incident_id, approver="alice")
        self.assertEqual(self._writes(), 0)

    def test_approved_proposal_cannot_be_rejected(self) -> None:
        inc = self._incident()
        self.orch.approve_remediation(inc.incident_id, approver="alice")
        with self.assertRaises(ProposalStateError):
            self.orch.reject_remediation(inc.incident_id)

    def test_failed_execution_is_reported_and_stays_retryable(self) -> None:
        inc = self._incident()
        with mock.patch.object(
            self.orch.ecs, "rollback_task_definition", side_effect=RemediationExecutionError("boom")
        ):
            with self.assertRaises(RemediationExecutionError):
                self.orch.approve_remediation(inc.incident_id, approver="alice")
        self.assertEqual(inc.proposal.status, "PENDING_APPROVAL")
        self.assertEqual(inc.audit_trail[-1]["step"], "remediation_failed")
        self.orch.approve_remediation(inc.incident_id, approver="alice")
        self.assertEqual(inc.status, "VERIFIED")

    def test_env_update_scenario_still_works_in_simulation(self) -> None:
        inc = self._incident("notifier", "secret-rotation-mismatch")
        self.assertEqual(inc.proposal.action_type, "UPDATE_SERVICE_ENV")
        self.orch.approve_remediation(inc.incident_id, approver="alice")
        self.assertEqual(inc.status, "VERIFIED")
        self.assertEqual(self._writes(), 2)  # register-task-definition + update-service


class TestLiveModeFailsClosed(_OrchestratorCase):
    def _token(self, uses: int = 1) -> str:
        return self.orch.approval_ledger.issue("PROP-T", "alice", uses=uses)

    def test_failed_aws_call_is_not_reported_as_success(self) -> None:
        self._incident()
        failing = {"allowed": True, "executed": True, "returncode": 1, "stderr": "AccessDenied"}
        with mock.patch.object(self.orch.cli_guard, "execute_command", return_value=failing):
            with self.assertRaises(RemediationExecutionError):
                self.orch.ecs.rollback_task_definition(
                    service="salescreate-api", target_revision="salescreate-api:14", approval_token=self._token()
                )
        self.assertEqual(self.orch.cloudwatch.get_service_scenario("salescreate-api"), "bad-release")

    def test_live_rollback_target_must_exist(self) -> None:
        self.orch.ecs.live_aws = True
        with mock.patch.object(self.orch.ecs, "_try_describe_live_task_definition", return_value=None):
            with self.assertRaises(RemediationExecutionError):
                self.orch.ecs.rollback_task_definition(
                    service="salescreate-api", target_revision="salescreate-api:14", approval_token=self._token()
                )

    def test_live_env_update_is_refused_instead_of_faked(self) -> None:
        self.orch.ecs.live_aws = True
        with self.assertRaises(RemediationExecutionError):
            self.orch.ecs.update_service_env(
                service="notifier", env_overrides={"FAULT_MODE": "healthy"}, approval_token=self._token(2)
            )

    def test_live_mode_proposes_rollback_not_env_update(self) -> None:
        inc = self._incident("notifier", "secret-rotation-mismatch")
        self.assertEqual(inc.proposal.action_type, "UPDATE_SERVICE_ENV")
        self.orch.ecs.live_aws = True
        proposal = self.orch.remediator.propose_remediation(inc.investigation, self.orch.ecs)
        self.assertEqual(proposal.action_type, "ROLLBACK_TASK_DEFINITION")

    def test_baseline_revision_can_be_overridden(self) -> None:
        with mock.patch.dict(os.environ, {"CLOUD_OPS_BASELINE_REVISIONS": "notifier=42, salescreate-api=7"}):
            self.assertEqual(self.orch.ecs.get_baseline_revision("notifier"), "notifier:42")
            self.assertEqual(self.orch.ecs.get_baseline_revision("salescreate-api"), "salescreate-api:7")
            self.assertEqual(self.orch.ecs.get_baseline_revision("inventory-worker"), "inventory-worker:9")


class _FakeCW:
    def __init__(self, metrics: dict) -> None:
        self._metrics = metrics

    def get_service_metrics(self, **_: object) -> dict:
        return dict(self._metrics)

    def get_recent_logs(self, **_: object) -> list:
        return []


class _FakeECS:
    def __init__(self, states: list[dict], live: bool) -> None:
        self.live_aws = live
        self._states = list(states)
        self.calls = 0

    def describe_ecs_service(self, **_: object) -> dict:
        self.calls += 1
        return dict(self._states.pop(0) if len(self._states) > 1 else self._states[0])


HEALTHY_METRICS = {
    "log_events": 20, "error_rate_pct": 0.0, "p95_latency_ms": 50, "memory_rss_mb": 100,
    "latency_reported": True, "memory_reported": True,
}
SETTLED = {
    "health_status": "HEALTHY", "rollout_state": "COMPLETED", "desired_count": 2, "running_count": 2,
    "active_task_definition": "salescreate-api:14",
}
ROLLING = {**SETTLED, "rollout_state": "IN_PROGRESS", "running_count": 1, "health_status": "DEGRADED"}


class TestVerifier(unittest.TestCase):
    PRE = {"error_rate_pct": 35.0, "p95_latency_ms": 80}

    def _verifier(self) -> tuple[VerifierAgent, list[float]]:
        now = [0.0]
        verifier = VerifierAgent(
            gemini_engine=mock.Mock(),
            settle_timeout_s=60,
            poll_interval_s=10,
            sleep=lambda secs: now.__setitem__(0, now[0] + secs),
            clock=lambda: now[0],
        )
        return verifier, now

    def test_missing_data_is_a_failure_not_a_pass(self) -> None:
        verifier, _ = self._verifier()
        report = verifier.verify_remediation("INC-X", "salescreate-api", self.PRE, _FakeCW({}), _FakeECS([{}], live=False))
        self.assertFalse(report.verified)
        self.assertFalse(report.rollback_triggered)
        failed = {c["name"] for c in report.checks if not c["passed"]}
        self.assertIn("telemetry_available", failed)
        self.assertIn("ecs_service_healthy", failed)
        self.assertIn("VERIFICATION FAILED", report.summary)

    def test_empty_log_window_is_not_healthy(self) -> None:
        verifier, _ = self._verifier()
        metrics = {**HEALTHY_METRICS, "log_events": 0}
        report = verifier.verify_remediation(
            "INC-X", "salescreate-api", self.PRE, _FakeCW(metrics), _FakeECS([SETTLED], live=False)
        )
        self.assertFalse(report.verified)

    def test_waits_for_rollout_to_settle_on_live_aws(self) -> None:
        verifier, now = self._verifier()
        ecs = _FakeECS([ROLLING, ROLLING, SETTLED], live=True)
        report = verifier.verify_remediation("INC-X", "salescreate-api", self.PRE, _FakeCW(HEALTHY_METRICS), ecs)
        self.assertTrue(report.verified, report.summary)
        self.assertEqual(ecs.calls, 3)
        self.assertEqual(now[0], 20.0)

    def test_gives_up_when_the_rollout_never_completes(self) -> None:
        verifier, now = self._verifier()
        report = verifier.verify_remediation(
            "INC-X", "salescreate-api", self.PRE, _FakeCW(HEALTHY_METRICS), _FakeECS([ROLLING], live=True)
        )
        self.assertFalse(report.verified)
        self.assertGreaterEqual(now[0], 60.0)
        self.assertIn("ecs_rollout_completed", report.summary)

    def test_simulation_does_not_wait(self) -> None:
        verifier, now = self._verifier()
        report = verifier.verify_remediation(
            "INC-X", "salescreate-api", self.PRE, _FakeCW(HEALTHY_METRICS), _FakeECS([SETTLED], live=False)
        )
        self.assertTrue(report.verified)
        self.assertEqual(now[0], 0.0)


class TestHTTPHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["CLOUD_OPS_OFFLINE_LLM"] = "1"
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        shutil.copytree(ROOT_DIR / "data", tmp / "data")
        (tmp / "artifacts").mkdir()
        orch = CloudOpsOrchestrator(config=AgentConfig(artifacts_dir=tmp / "artifacts", data_dir=tmp / "data"))
        cls.server = create_server(host="127.0.0.1", port=0, orchestrator=orch)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.pop("CLOUD_OPS_OFFLINE_LLM", None)
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def _call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None,
              host: str | None = None) -> tuple[int, dict, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.putrequest(method, path, skip_host=host is not None)
            if host is not None:
                conn.putheader("Host", host)
            payload = json.dumps(body).encode() if body is not None else None
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            if payload is not None:
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders(payload)
            resp = conn.getresponse()
            raw = resp.read()
            data = json.loads(raw) if raw and resp.getheader("Content-Type", "").startswith("application/json") else {}
            return resp.status, data, {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()

    def test_cross_site_writes_are_blocked(self) -> None:
        status, _, headers = self._call("POST", "/api/conversations", {"title": "x"}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", headers)
        status, _, _ = self._call("POST", "/api/conversations", {"title": "x"}, {"Origin": "null"})
        self.assertEqual(status, 403)
        status, _, _ = self._call("OPTIONS", "/api/chat", None, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_same_origin_and_non_browser_clients_still_work(self) -> None:
        status, _, _ = self._call("POST", "/api/conversations", {"title": "a"}, {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        status, _, headers = self._call("POST", "/api/conversations", {"title": "b"})
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_allow_listed_origin_gets_cors_headers(self) -> None:
        with mock.patch.dict(os.environ, {"CLOUD_OPS_ALLOWED_ORIGINS": "https://ui.example.com"}):
            status, _, headers = self._call(
                "POST", "/api/conversations", {"title": "c"}, {"Origin": "https://ui.example.com"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "https://ui.example.com")

    def test_dns_rebinding_host_is_rejected(self) -> None:
        status, _, _ = self._call("GET", "/api/health", host="evil.example")
        self.assertEqual(status, 403)
        status, _, _ = self._call("GET", "/api/health", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)

    def test_health_does_not_leak_project_or_credential_paths(self) -> None:
        status, data, _ = self._call("GET", "/api/health")
        self.assertEqual(status, 200)
        llm = data["llm_status"]
        self.assertNotIn("vertex_project", llm)
        self.assertNotIn("adc_credentials_path", llm)
        self.assertIn("model", llm)

    def test_double_approve_returns_conflict(self) -> None:
        status, data, _ = self._call("POST", "/api/investigate", {"service": "salescreate-api", "scenario": "bad-release"})
        self.assertEqual(status, 200)
        incident_id = data["incident_id"]
        status, _, _ = self._call("POST", f"/api/incidents/{incident_id}/approve", {"approver": "alice"})
        self.assertEqual(status, 200)
        status, body, _ = self._call("POST", f"/api/incidents/{incident_id}/approve", {"approver": "alice"})
        self.assertEqual(status, 409)
        self.assertIn("PENDING_APPROVAL", body["error"])
        status, _, _ = self._call("POST", f"/api/incidents/{incident_id}/reject", {})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
