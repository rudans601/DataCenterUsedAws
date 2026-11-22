import json
import subprocess
import unittest
from unittest.mock import patch

from eval import cluster_fixtures, observation_capture
from kubeops_copilot.observations import replay
from kubeops_copilot.scenarios import single_plan, single_timeline


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.start = "2026-11-22T00:00:00+00:00"
        self.end = "2026-11-22T00:00:06+00:00"
        self.incident = {"incident_id": "x", "target": "demo/api", "cluster": "test-cluster",
                         "start": self.start, "end": self.end, "control": []}
        self.http = {"cluster": "test-cluster", "target": "demo/api", "source": "traffic/http-observer",
                     "start": self.start, "at": self.end, "codes": [200]*4, "latencies_ms": [2]*4}

    def test_api_timeout_is_not_an_application_failure(self):
        token = cluster_fixtures.control_log.set(self.incident["control"])
        try:
            with patch("eval.observation_capture.now", return_value=self.end), \
                    patch("eval.cluster_fixtures.now", return_value=self.end), \
                    patch.object(observation_capture.subprocess, "run", side_effect=subprocess.TimeoutExpired("get", 3)):
                observation_capture.check_read()
        finally:
            cluster_fixtures.control_log.reset(token)
        score = replay(self.incident, [json.dumps(self.http)])
        self.assertEqual(score["http"], "normal")
        self.assertEqual(score["control"], {"read": {"timeout": 1}, "write": {}, "new_pods": None})

    def test_outside_window_other_cluster_and_duplicates(self):
        foreign = dict(self.http, cluster="other")
        future = dict(self.http, at="2026-11-22T00:00:07+00:00")
        lines = [json.dumps(x) for x in (foreign, future)]
        self.assertEqual(replay(self.incident, lines)["http"], "unknown")
        hit = json.dumps(dict(self.http, codes=[0, 200, 200, 503]))
        score = replay(self.incident, lines+[hit, hit])
        self.assertEqual(score["http"], "incident")
        self.assertEqual(score["response_codes"], {0: 1, 200: 2, 503: 1})

    def test_scheduling_distinguishes_absent_pending_and_assigned(self):
        old = {"uid": "a", "node": "worker-a"}
        placement = {"stage": "placement", "target": "demo/api", "result": "observed",
                     "at": self.start, "pods": [old]}
        self.incident.update(baseline_pod_uids=["a"], control=[placement])
        for pods, expected in [([old], (0, 0)), ([old, {"uid": "b", "node": None}], (1, 0)),
                               ([old, {"uid": "b", "node": "worker-b"}], (1, 1))]:
            self.incident["control"] = [placement, dict(placement, at=self.end, pods=pods)]
            score = replay(self.incident, [])["control"]["new_pods"]
            self.assertEqual((score["created"], score["assigned"]), expected)

    def test_remeasurement_waits_for_another_worker_request(self):
        item = dict(self.http, client={"at": self.start})
        next_item = dict(item, client={"at": self.end})
        with patch.dict(observation_capture.os.environ, KUBEOPS_EKS_CREATED_AT="test-cluster"), \
                patch.object(observation_capture, "now", return_value=self.end), \
                patch.object(observation_capture.time, "sleep"), \
                patch.object(observation_capture, "kubectl", side_effect=map(json.dumps, [item, next_item])):
            self.assertEqual(observation_capture.current_client(after=self.start), next_item)

    def test_generator_is_repeatable_without_changing_catalog(self):
        a,b = single_plan(42),single_plan(43)
        self.assertEqual(a, single_plan(42))
        self.assertEqual(len({c["case"] for c in a}),14)
        self.assertNotEqual(a,b)
        self.assertEqual([single_timeline(c) for c in a], [single_timeline(c) for c in single_plan(42)])
