from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from eval import eks_replay
from kubeops_copilot import control_replay as control
from kubeops_copilot.campaign import campaign_plan
from kubeops_copilot.detection import PROTOCOL, decision_matrix, experiment_impact
from kubeops_copilot.observations import instant, replay
from kubeops_copilot.scenarios import single_plan


class ControlReplayTests(unittest.TestCase):
    end = datetime(2026, 11, 22, 6, tzinfo=timezone.utc)

    def case(self, name, **changes):
        case = control.plan_case(42, name)
        case["conditions"].update(changes)
        return case

    def window(self, name, **changes):
        return control.make_window(self.case(name, **changes), "case-id", "cluster-id", self.end)

    def packet(self, seconds, codes=None, latency=10):
        at = (self.end - timedelta(seconds=seconds)).isoformat()
        return json.dumps({"source": "traffic/http-observer", "cluster": "cluster-id", "target": "demo/api",
                           "start": at, "at": at, "codes": codes or [200]*4, "latencies_ms": [latency]*4,
                           "client": {"at": at, "ms": latency, "code": 200}})

    def score(self, window, *lines):
        return replay(window, lines)["reference"]

    def test_catalog_and_campaign(self):
        local = single_plan(42)
        eks = single_plan(42, True)
        self.assertEqual(18, len({c["case"] for c in eks}))
        self.assertEqual(local, [c for c in eks if "mode" not in c])
        self.assertEqual(eks, single_plan(42, True))
        campaign = campaign_plan(42, include_replay=True)
        self.assertEqual(Counter(c["target_tier"] for c in campaign), dict(healthy=35, warning=45, degraded=15, critical=5))
        self.assertEqual(set(control.NAMES), {c["case"] for c in campaign if c.get("mode")})

    def test_two_paths_and_single_path(self):
        for sites in (["운영자 PC"], ["VPC 내부"], ["VPC 내부", "운영자 PC"]):
            w = self.window("api-timeout", sites=sites, recovers=False)
            ref = self.score(w, self.packet(2))
            self.assertEqual(ref["route"], "handoff")
            self.assertIn("양쪽" if len(sites) == 2 else sites[0], ref["reason"])
            self.assertFalse(any(e["stage"] == "write" for e in w["control"]))

    def test_pending_vs_uncreated_and_lease_evidence(self):
        for visible in (False, True):
            w = self.window("scheduler-stall", lease_visible=visible, new_pods=3, recovers=False)
            out = replay(w, [self.packet(2)])
            self.assertEqual({"created": 3, "assigned": 0}, out["control"]["new_pods"])
            self.assertIn("스케줄러 이상 의심" if visible else "원인 미확정", out["reference"]["reason"])
        w = self.window("controller-lag", replicas=[3, 5], recovers=False)
        out = replay(w, [self.packet(2)])
        self.assertEqual({"created": 0, "assigned": 0}, out["control"]["new_pods"])
        self.assertIn("생성 지연", out["reference"]["reason"])

    def test_quota_alarm_and_read_after_write(self):
        for alarm in (False, True):
            w = self.window("etcd-quota", alarm_visible=alarm, recovers=False)
            ref = self.score(w, self.packet(1))
            self.assertIn("조회 성공·쓰기 실패", ref["reason"])
            self.assertEqual(alarm, "NOSPACE" in ref["reason"])
        w = self.window("etcd-quota", recovers=True)
        w["control"] = [e for e in w["control"] if not e.get("change_confirmed")]
        self.assertEqual("handoff", self.score(w, self.packet(1))["route"])

    def test_grade_and_route_are_independent(self):
        for name in control.NAMES:
            for recovers in (True, False):
                w = self.window(name, recovers=recovers)
                ref = self.score(w, self.packet(12), self.packet(1))
                self.assertEqual("warning", ref["impact"])
                self.assertEqual("observe" if recovers else "handoff", ref["route"])
                self.assertEqual("unknown", self.score(w)["impact"])
                # A success recorded before recovery cannot confirm recovery.
                self.assertEqual("handoff", self.score(w, self.packet(10))["route"])

    def test_outage_needs_correlated_endpoint_and_two_failed_batches(self):
        w = self.window("api-timeout")
        failed = [self.packet(t, [503]*4) for t in (6, 2)]
        self.assertEqual("degraded", self.score(w, *failed)["impact"])
        w["eks_endpoints"] = {"at": self.end.isoformat(), "ready": 0, "source": "eks-endpointslices"}
        self.assertEqual("critical", self.score(w, *failed)["impact"])
        self.assertEqual("degraded", self.score(w, failed[0], failed[0])["impact"])
        w["eks_endpoints"].update(at=w["start"])
        self.assertEqual("degraded", self.score(w, *failed)["impact"])
        self.assertEqual("warning", self.score(w, self.packet(2, latency=300))["impact"])
        self.assertEqual("degraded", self.score(w, self.packet(2, latency=301))["impact"])
        self.assertEqual("degraded", self.score(w, self.packet(2, [503, 200, 200, 200]))["impact"])
        self.assertEqual("unknown", self.score(w, self.packet(1000))["impact"])

    def test_blind_model_and_report_matrix(self):
        case = self.case("api-timeout", sites=["운영자 PC"], recovers=False)
        row = dict(case, protocol=PROTOCOL, incident_id="blind")
        prediction = {"service_detection": "normal", "impact": "warning", "route": "observe",
                      "proposal": {"action": "none"}, "summary": "test", "decision_reason": "test",
                      "root_cause": "test", "manual_steps": []}
        with patch.dict(eks_replay.os.environ, KUBEOPS_EKS_CREATED_AT="cluster-id"), \
                patch.object(eks_replay, "datetime") as clock, patch.object(eks_replay, "endpoint_sample", return_value=None), \
                patch.object(eks_replay, "observer_logs", return_value=[self.packet(2)]), \
                patch.object(eks_replay.graph, "retrieve", return_value={"docs": []}), \
                patch.object(eks_replay.graph, "analyze", return_value={"analysis": prediction}) as model, \
                patch.object(eks_replay.graph, "execute", side_effect=AssertionError("replay executed a change")):
            clock.now.return_value = self.end
            eks_replay.evaluate_replay(case, row, False)
        payload = json.dumps(model.call_args.args[0])
        for label in ("api-timeout", "recovers", "reference", "trial_seed", "target_tier"):
            self.assertNotIn(label, payload)
        self.assertEqual(row["probes"], json.loads(model.call_args.args[0]["evidence"][1]["detail"]))
        self.assertEqual(("warning", True, False), (experiment_impact(row)[0], row["impact_correct"], row["route_correct"]))
        self.assertEqual(1, decision_matrix([row], "route")[0]["handoff", "observe"])
