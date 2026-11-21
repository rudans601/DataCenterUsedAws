import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.incidents_ui import handle_demo_request, publish_decision
from eval import single_detection
from kubeops_copilot import audit, detection, first_response, graph, incidents


class AppRecoveryTests(unittest.TestCase):
    def echo(self, responses):
        with patch.object(first_response.requests, "post", side_effect=responses) as send, patch.object(first_response.time, "sleep"):
            result = first_response.echo("hello")
            bodies = [call.kwargs["data"] for call in send.call_args_list]
            self.assertEqual(bodies, [b"hello"] * len(responses))
            return result

    def test_echo_retries_only_transient_errors_and_checks_response(self):
        success = Mock(status_code=200)
        success.json.return_value = ["hello"]
        self.assertEqual(self.echo([success])["outcome"], "healthy")
        self.assertEqual(self.echo([Mock(status_code=503), success])["outcome"], "recovered")
        self.assertEqual(self.echo([first_response.requests.Timeout(), Mock(status_code=502)])["outcome"], "escalated")
        self.assertEqual(self.echo([Mock(status_code=403)])["outcome"], "escalated")
        success.json.return_value = ["other"]
        self.assertEqual(self.echo([success])["outcome"], "escalated")

    def test_remaining_cluster_symptoms_and_later_failures_reach_agent(self):
        sample = dict(api_code=200, echo_ok=True, endpoints=2, ready=2, desired=2,
                      p95_ms=3, error_ratio=0, observation_started=100, last_pod_warning=90)
        checks = [{"outcome": "recovered"}]
        readings = [{"code": 200, "ms": 3}]
        self.assertTrue(single_detection.app_resolved(checks, [sample], readings))
        for anomaly in [{"ready": 1}, {"p95_ms": None}, {"last_pod_warning": 101}, {"dns_error": "NXDOMAIN"}]:
            self.assertFalse(single_detection.app_resolved(checks, [{**sample, **anomaly}], readings))
        self.assertFalse(single_detection.app_resolved(checks, [sample], readings + [{"code": 503, "ms": 8}]))

    def test_app_resolution_never_earns_model_credit(self):
        row = dict(fixtures_only=False, protocol=detection.PROTOCOL, measurement_status="complete",
                   truth="normal", prediction_status="app_recovered")
        self.assertEqual(detection.bucket(row), "앱 1차 복구")
        self.assertIsNone(detection.statistics([row])[1]["판정 완료율"])
        self.assertEqual(detection.decision_matrix([row], "route"), ({}, 0))


class TicketTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for ctx in [patch.object(audit, "settings", SimpleNamespace(db_path=self.root / "test.db")),
                    patch.object(incidents, "notify", return_value="알림함 저장")]:
            self.addCleanup(ctx.stop)
            self.notification = ctx.start()

    def test_retrying_handoff_notifies_once_and_retains_actor(self):
        number = incidents.issue("incident", "사람 인계", "Service 설정", {"대상": "demo/api"})
        self.assertEqual(number, incidents.issue("incident", "사람 인계", "Service 설정", {}))
        self.notification.assert_called_once_with(number)
        self.assertFalse(incidents.transition(number, "처리 완료", "Lee", "조건 미확인"))
        self.assertTrue(incidents.transition(number, "처리 중", "Lee", "접수"))
        self.assertTrue(incidents.transition(number, "처리 완료", "Lee", "selector 수정 후 응답 확인"))
        self.assertEqual(incidents.tickets()[0]["status"], "처리 완료")
        self.assertEqual([e["actor"] for e in incidents.timeline("incident")], ["시스템", "시스템", "Lee", "Lee"])

    def test_only_unresolved_app_requests_invoke_agent_and_ticket_on_failure(self):
        attempts = [{"attempt": 1, "at":"2026-01-01", "detail":"HTTP 503", "ms":4}]
        with patch.object(first_response, "echo") as echo, patch.object(graph, "ask", side_effect=TimeoutError()) as ask:
            echo.return_value = {"outcome":"recovered", "attempts":attempts}
            self.assertIsNone(handle_demo_request("resolved", "private input"))
            ask.assert_not_called()
            echo.return_value = {"outcome":"escalated", "attempts":attempts}
            self.assertIsNone(handle_demo_request("failed", "private input"))
            self.assertEqual(ask.call_args.kwargs["thread_id"], "failed")
            self.assertNotIn("private input", str(ask.call_args))
            self.assertEqual([t["kind"] for t in incidents.tickets("failed")], ["진단 실패"])

    def test_operator_closes_manual_ticket_from_inbox_with_a_note(self):
        from streamlit.testing.v1 import AppTest
        number = incidents.issue("inbox", "사람 인계", "서비스 경로 확인", {})
        app = AppTest.from_string("import streamlit as st\nfrom app.incidents_ui import inbox\nst.session_state.approver='Lee'\ninbox()").run()
        app.button(key=f"accept_{number}").click().run()
        self.assertEqual(incidents.tickets("inbox")[0]["status"], "처리 중")
        app.text_input[0].input("selector 수정 후 확인")
        next(button for button in app.button if button.label == "처리 완료 기록").click().run()
        self.assertEqual(incidents.tickets("inbox")[0]["status"], "처리 완료")
        self.assertFalse(app.exception)

    def test_cost_blocks_auto_and_approved_scaling(self):
        p = {"action":"scale", "namespace":"demo", "deployment":"api", "replicas":3}
        state = {"thread_id":"billable", "snapshot":{"deployments":[{"name":"api","desired":2}]},
                 "proposal":p, "analysis":{"route":"auto", "summary":"증설 필요", "billable_change":False}}
        decision = {"route":"auto", "impact":"degraded", "reason":"런북 조건 일치"}
        with patch.object(graph.actions, "check", return_value={"normalized":p, "target_version":{}}), \
             patch.object(graph.triage, "classify", return_value=decision), patch.object(graph, "local_auto_enabled", return_value=True), \
             patch.dict(graph.os.environ, {"KUBEOPS_BILLABLE_CAPACITY":"1"}), patch.object(graph.actions, "execute") as write:
            state.update(graph.guard(state))
            self.assertEqual(state["triage"]["route"], "handoff")
            self.assertIn("비용", incidents.tickets()[0]["reason"])
            with self.assertRaises(ValueError):
                graph.execute(state)
            with patch.object(graph.k8s_tools, "deployment_snapshot", return_value={"desired":2}):
                changed = {**state, "triage":{"route":"approval"}, "approval":{"approved":True, "approver":"Lee"}}
                self.assertFalse(graph.execute(changed)["execution"]["ok"])
            write.assert_not_called()

    def test_only_first_decision_for_current_proposal_is_delivered(self):
        request = {"id":"abc"}
        pending = self.root / "pending.json"
        pending.write_text(json.dumps(request))
        self.assertTrue(publish_decision(self.root, request, False, "Lee"))
        self.assertFalse(publish_decision(self.root, request, True, "Kim"))
        self.assertEqual(json.loads((self.root / "abc.json").read_text()), {"approved":False,"approver":"Lee"})
        pending.unlink()
        self.assertFalse(publish_decision(self.root, {"id":"stale"}, True, "Kim"))
