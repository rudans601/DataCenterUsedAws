import unittest
from unittest.mock import patch

from eval.mentoring import confirmed
from kubeops_copilot.scenarios import NAMES, plan
from kubeops_copilot import graph, triage


class MentoringTests(unittest.TestCase):
    def setUp(self):
        self.version = {"uid": "u", "generation": 9, "revision": 3}
        self.snapshot = {"namespace": "demo", "deployments": [{"name": "api", "uid": "u", "revision": "3",
                         "observed_generation": 9, "generation": 9, "ready": 2, "desired": 2, "updated": 2, "available": 2}],
                         "services": [{"name": "api", "type": "ClusterIP", "selector": {"app": "api"},
                                       "ready_endpoints": 2, "matching_pods": 2}],
                         "metrics": {"api": {"rps": 20, "error_ratio": 0, "p95_ms": 30}}, "events": []}

    def decision(self, action="none", route="observe", impact="warning", **kwargs):
        proposal = {"action": action, "namespace": "demo", "deployment": "api", "replicas": 3}
        checked = {"normalized": proposal, "executable": action not in ("none", "manual"),
                   "allowed": action not in ("none", "manual"), "target_version": self.version}
        analysis = {"confidence": "high", "service_detection": "incident" if impact == "degraded" else "normal", "proposal": proposal, "route": route, "impact": impact,
                    "target_grounded": True, "cited_procedures": [f"{route}.{action}"], "citation_source": "knowledge/runbooks/v2/release-latency.md",
                    "decision_reason": "관측과 런북 대조", **kwargs}
        return triage.classify(self.snapshot, analysis, checked, local=True)

    def test_reproducible_plan(self):
        self.assertEqual(plan(42), plan(42))
        self.assertEqual({c["case"] for c in plan(42)}, set(NAMES))
        self.assertEqual(len(plan(42)), 14)
        self.assertNotEqual(plan(42), plan(7))

    def test_service_outage_is_visible_when_web_still_echoes(self):
        sample = {"desired": 2, "ready": 2, "endpoints": 0, "api_code": -1, "echo_ok": True}
        self.assertTrue(confirmed(sample, "service-broken"))
        self.assertFalse(confirmed(sample, "baseline"))
        self.assertFalse(confirmed(dict(sample, api_code=200), "service-broken"))

    def test_storage_pending_requires_both_signals(self):
        sample = {"claim_phase": "Pending", "pod_events": ["FailedScheduling Insufficient cpu"]}
        self.assertFalse(confirmed(sample, "pvc"))
        self.assertTrue(confirmed(sample, "pending"))
        sample["pod_events"] = ["FailedScheduling pod has unbound immediate PersistentVolumeClaims"]
        self.assertTrue(confirmed(sample, "pvc"))
        self.assertFalse(confirmed(sample, "pending"))

    def test_gap_requires_working_monitoring_and_service(self):
        sample = {"prometheus_up": False, "api_code": 200, "rps": None, "p95_ms": None}
        self.assertFalse(confirmed(sample, "metrics-gap"))
        sample["prometheus_up"] = True
        self.assertTrue(confirmed(sample, "metrics-gap"))

    def test_dns_requires_direct_ip_success(self):
        sample = {"dns_error": "lookup: no such host", "ip_probe": -1}
        self.assertFalse(confirmed(sample, "dns"))
        sample["ip_probe"] = 200
        self.assertTrue(confirmed(sample, "dns"))

    def test_warning_observed(self):
        self.snapshot["events"] = [{"type": "Warning", "object": "api"}]
        decision = self.decision()
        self.assertEqual((decision["impact"], decision["route"]), ("warning", "observe"))

    def test_healthy_warning_does_not_request_suggested_restart(self):
        self.snapshot["events"] = [{"type": "Warning", "object": "api"}]
        result = self.decision("restart")
        self.assertEqual(result["model_route"], "observe")
        self.assertEqual(result["route"], "handoff")

    def test_rollback_requires_approval(self):
        self.snapshot["metrics"]["api"]["p95_ms"] = 700
        decision = self.decision("rollback", "approval", "degraded")
        self.assertEqual((decision["impact"], decision["route"]), ("degraded", "approval"))
        self.assertEqual(graph._after_guard({"triage": decision}), "approval")
        self.assertEqual(graph._after_approval({"approval": {"approved": False}}), "report")

    def test_selector_mismatch_cannot_execute_restart(self):
        self.snapshot["services"][0].update(ready_endpoints=0, matching_pods=0)
        decision = self.decision("restart", "approval", "critical")
        self.assertEqual((decision["model_route"], decision["route"]), ("approval", "handoff"))
        self.assertEqual(graph._after_guard({"triage": decision}), "report")

    def test_missing_observations_need_review(self):
        decision = self.decision("restart", "approval", target_grounded=False)
        self.assertEqual(decision["route"], "handoff")
        self.assertEqual(self.decision("restart", "approval", citation_source=None)["route"], "handoff")

    def test_same_observations_preserve_different_model_decisions(self):
        for route, action in [("observe", "none"), ("handoff", "manual"), ("approval", "rollback")]:
            self.assertEqual(self.decision(action, route, root_cause_category="bad_deploy")["route"], route)

    def test_auto_scale_requires_two_windows_capacity_and_allowed_single_increment(self):
        self.snapshot["metrics"]["api"].update(cpu_util=.90, p95_ms=500)
        self.snapshot["recovery_facts"] = {"previous_p95_ms": 450, "spare_worker": True}
        source = triage.AUTO_PROCEDURES["scale"]
        def decide():
            return self.decision("scale", "auto", "degraded", citation_source="knowledge/runbooks/v2/" + source)
        self.assertEqual(graph._after_guard({"triage": decide()}), "execute")
        self.snapshot["recovery_facts"]["previous_p95_ms"] = 25
        self.assertEqual(decide()["route"], "handoff")
        self.snapshot["recovery_facts"].update(previous_p95_ms=450, spare_worker=False)
        self.assertEqual(decide()["route"], "handoff")
        self.snapshot["recovery_facts"]["spare_worker"] = True
        self.snapshot["deployments"][0]["desired"] = 3
        self.assertEqual(decide()["route"], "handoff")

    def test_fixture_result_uses_observations_not_name(self):
        row = {"case": "normal", "measurement_status": "complete", "injected": {"ready": 0, "desired": 2, "endpoints": 0},
               "probes": [{"count": 4, "failed": 4}] * 2}
        self.assertEqual(triage.experiment_impact(row)[0], "critical")
        row["measurement_status"] = "timeout"
        self.assertEqual(triage.experiment_impact(row)[0], "unknown")

    def test_recovered_startup_probe_leaves_healthy_control(self):
        row = {"measurement_status": "complete", "injected": {
            "ready": 1, "desired": 1, "api_code": 200, "endpoints": 1,
            "pod_events": ["Unhealthy Readiness probe failed: Get /readyz: connect: connection refused"]},
            "probes": [{"count": 4, "failed": 0, "p95_ms": 1}]}
        self.assertEqual(triage.experiment_impact(row)[0], "healthy")
        row["injected"]["warning_seen"] = True
        self.assertEqual(triage.experiment_impact(row)[0], "warning")

    def test_verification_requires_service_recovery(self):
        before = {"metrics": {"rps": 10, "error_ratio": 1, "p95_ms": 850}}
        state = {"before": before, "execution": {"ok": True, "action": "rollback", "namespace": "demo", "deployment": "api",
                                                 "uid": "api-uid", "generation": 5}}
        for error, latency, traffic, expected in [(None, 5, 10, "unknown"), (0, None, 10, "unknown"),
                (0, 5, 0, "unknown"), (0.5, 5, 10, "partial"), (0, 820, 10, "partial"), (0, 5, 10, "success")]:
            after = {"ready": 2, "desired": 2, "updated": 2, "available": 2, "problem_pods": 0,
                     "uid": "api-uid", "generation": 5, "observed_generation": 5,
                     "metrics": {"rps": traffic, "error_ratio": error, "p95_ms": latency}}
            with self.subTest(error=error, latency=latency, traffic=traffic), patch.object(graph, "_wait_rollout", return_value=(True, 0)), \
                    patch.object(graph.time, "sleep"), patch.object(graph.k8s_tools, "deployment_snapshot", return_value=after):
                self.assertEqual(graph.verify(state)["verification"]["verdict"], expected)
