"""같은 주입·서비스 관측을 독립 채점하고, 웹이 사용하는 혼동행렬까지 확인한다."""
from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

from eval import single_detection as runner

from eval.remediation_score import reference_decision
from kubeops_copilot.detection import PROTOCOL, decision_matrix
from kubeops_copilot.scenarios import NAMES


class EnvironmentReferenceTests(unittest.TestCase):
    def setUp(self):
        self.conditions = {"pattern": "plateau", "visibility": "all"}
        self.row = {"truth": "incident", "injected": {"desired": 2, "ready": 2},
                    "episodes": [{"measured_until": "2026-11-22T12:00:00+00:00"}]}
        self.metrics = {"p95_ms": 450, "cpu_util": .9, "error_ratio": .01}
        self.facts = {"spare_worker": True, "previous_p95_ms": 420, "previous_revision": 2,
                      "previous_available": 2, "only_args_or_readiness": True, "new_pods": 1, "new_ready": 0}
        self.snapshot = {"metrics": {"api": self.metrics}, "recovery_facts": self.facts,
                         "observations": {"persistent_gap": True}}

    def reference(self, name, env):
        return reference_decision({"case": name, "conditions": self.conditions}, self.row, self.snapshot,
                                  environment=env)

    def check_matrix(self, name, env, expected, alternative):
        ref = self.reference(name, env)
        self.assertEqual((ref["route"], ref["action"]), expected)
        self.assertEqual(ref["environment"], env)
        row = {**self.row, "protocol": PROTOCOL, "reference": ref, "effective_route": expected[0],
               "proposal": {"namespace": "demo", "deployment": "api", "replicas": 3}}
        rows = [dict(row, model_route=route, proposed_action=action) for route, action in (expected, alternative)]
        self.assertEqual(decision_matrix(rows, "route"), ({(expected[0], expected[0]): 1,
                                                            (expected[0], alternative[0]): 1}, 0))
        for result, field in zip(rows, (expected, alternative)):
            self.assertEqual(decision_matrix([result], "action"), ({(expected[1], field[1]): 1}, 0))

    def test_sustained_load_same_evidence_different_grade(self):
        self.check_matrix("high-load", "eks", ("handoff", "manual"), ("auto", "scale"))
        self.check_matrix("high-load", "kind", ("auto", "scale"), ("handoff", "manual"))
        self.facts["spare_worker"] = False
        self.assertEqual(self.reference("high-load", "eks")["route"], "handoff")
        self.assertIsNone(self.reference("high-load", "kind")["route"])
        self.metrics["cpu_util"] = .25
        self.assertIsNone(self.reference("high-load", "eks")["route"])

    def test_isolated_rollout_same_evidence_different_grade(self):
        self.row["truth"] = "normal"
        for name in ("crashloop", "oom", "probe-fail"):
            with self.subTest(name=name):
                self.check_matrix(name, "eks", ("approval", "rollback"), ("auto", "rollback"))
                self.check_matrix(name, "kind", ("auto", "rollback"), ("approval", "rollback"))

    def test_local_auto_limits_do_not_hide_eks_approval_or_cost_review(self):
        self.facts["last_auto_at"] = "2026-11-22T11:59:00+00:00"
        self.assertEqual(self.reference("high-load", "eks")["route"], "handoff")
        self.assertIsNone(self.reference("high-load", "kind")["route"])
        self.conditions["visibility"] = "no_request_metrics"
        self.row["truth"] = "normal"
        self.assertEqual(self.reference("probe-fail", "eks")["route"], "approval")
        self.assertIsNone(self.reference("probe-fail", "kind")["route"])

    def test_runner_uses_selected_environment_before_diagnosis(self):
        def measured(case, row, evidence, end, sample):
            row["_snapshot"] = self.snapshot
            latency = 450 if case["case"] == "high-load" else 12
            row["probes"].append({"count": 4, "failed": 0, "p95_ms": latency})

        for env in ("kind", "eks"):
            for name in ("high-load", "probe-fail"):
                with self.subTest(env=env, name=name):
                    case = {"case": name, "qps": 600, "conditions": {
                        **self.conditions, "repeat": 1, "onset_s": 0, "duration_s": 10}}
                    row = {"observation_windows": [{"control": []}]}
                    # I/O만 대체하고 실제 평가기의 환경 선택, HTTP 정답 계산, 참조 저장 경로를 통과한다.
                    with patch.multiple(runner, EKS_LAB=env == "eks", prepare=Mock(), rollout=Mock(),
                                        wait_sample=Mock(return_value={}), node_capacity=Mock(),
                                        resource=Mock(return_value={"spec": {"selector": {"app": "api"}}}),
                                        inject=Mock(return_value=nullcontext({})), capture=Mock(return_value=nullcontext()),
                                        api_pods=Mock(return_value=[]), establish=Mock(return_value=self.row["injected"]),
                                        observe=Mock(return_value=self.row["injected"]),
                                        traffic_pulse=Mock(return_value=nullcontext(1)), measure=measured):
                        runner.evaluate(case, row, True, None, False)
                    expected = "auto" if env == "kind" else {"high-load": "handoff", "probe-fail": "approval"}[name]
                    self.assertEqual((row["reference"]["environment"], row["reference"]["route"]), (env, expected))
                    self.assertNotIn("model_route", row)

    def test_unverified_revision_and_missing_http(self):
        for env in ("kind", "eks"):
            self.facts["previous_revision"] = None
            self.row["truth"] = "incident"
            for name in ("bad-deploy", "image-pull", "probe-fail"):
                self.assertEqual(self.reference(name, env)["route"], "handoff")
            self.row["truth"] = "unknown"
            self.assertIsNone(self.reference("probe-fail", env)["route"])

    def test_coverage_and_common_routes(self):
        routes = {"normal": "observe", "transient-warning": "observe", "maintenance": "observe",
                  "dns": "handoff", "pvc": "handoff", "pending": "handoff", "service-broken": "handoff",
                  "metrics-gap": "handoff", "image-pull": "approval", "bad-deploy": "approval"}
        self.assertEqual(set(routes) | {"probe-fail", "oom", "crashloop", "high-load"}, set(NAMES))
        for env in ("kind", "eks"):
            for name, route in routes.items():
                self.row["truth"] = "incident" if name == "bad-deploy" else "normal"
                self.assertEqual(self.reference(name, env)["route"], route, name)
            self.row["truth"] = "normal"
            self.assertEqual(self.reference("high-load", env)["route"], "observe")
        with self.assertRaises(ValueError):
            self.reference("normal", "other")
