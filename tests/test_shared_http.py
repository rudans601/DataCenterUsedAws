"""요청을 다시 보내지 않고 같은 관측으로 채점·진단하며, 정답은 숨긴다."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from eval import eks_replay, single_detection as runner
from kubeops_copilot import control_replay, detection, graph
from kubeops_copilot.observations import HTTP_OBSERVATION_VERSION, http_evidence


def batch(ms=12, failed=False, at="2026-11-24T06:00:00+00:00"):
    return dict(start=at, at=at, source="traffic/http-observer", cluster="test-cluster", target="demo/api",
                codes=[503 if failed else 200, 200, 200, 200], latencies_ms=[ms, 10, 11, 9],
                count=4, failed=int(failed), p95_ms=max(ms, 11))


class SharedHTTPTests(unittest.TestCase):
    def test_allowlist_drops_answers_and_freezes_payload(self):
        measured = batch()
        sample = dict(measured, reference={"impact": "SECRET", "route": "SECRET"}, truth="SECRET",
                      target_tier="SECRET", case="SECRET", trial_seed="SECRET")
        evidence = http_evidence([sample])
        self.assertEqual(json.loads(evidence["detail"]), [measured])
        self.assertNotIn("SECRET", json.dumps(evidence))
        sample["latencies_ms"][0] = 999
        self.assertEqual(json.loads(evidence["detail"])[0]["latencies_ms"][0], 12)
        self.assertEqual(json.loads(http_evidence([])["detail"]), [])

    def test_workload_shares_every_batch_including_spike_and_failure(self):
        for visibility in ("all", "no_request_metrics", "no_app_logs"):
            for abnormal in (batch(2506.76), batch(12, failed=True)):
                samples = [abnormal, batch()]
                row, evidence = {"probes": []}, []
                with patch.object(runner, "oracle_probe", side_effect=samples) as collect, \
                        patch.object(runner, "model_evidence", return_value=[]), \
                        patch.object(runner.time, "monotonic", side_effect=[0, 0, 0, 2]), \
                        patch.object(runner.time, "sleep"), patch.object(runner, "current_client") as other:
                    runner.measure({"conditions": {"visibility": visibility}}, row, evidence, 1, {})
                self.assertEqual(collect.call_count, 2)
                other.assert_not_called()
                self.assertEqual(row["probes"], samples)
                self.assertEqual(json.loads(evidence[-1]["detail"]), row["probes"])
                self.assertEqual(detection.ground_truth(row["probes"]), "incident")
                self.assertEqual(row["http_observation_version"], HTTP_OBSERVATION_VERSION)

    def test_recheck_keeps_initial_evidence_and_uses_separate_saved_batches(self):
        initial = batch(2506.76)
        evidence = [http_evidence([initial])]
        followups = [batch(at=f"2026-11-24T06:00:0{i}+00:00") for i in (1, 2, 3)]
        recorded = []
        first = {"service_detection": "unknown", "impact": "unknown"}
        final = {"service_detection": "incident", "impact": "degraded"}
        with patch.object(runner, "oracle_probe", side_effect=followups), \
                patch.object(runner, "model_evidence", return_value=[]), \
                patch.object(graph, "retrieve", return_value={"docs": []}), \
                patch.object(graph, "analyze", side_effect=[{"analysis": first}, {"analysis": final}]) as model, \
                patch.object(graph, "guard", return_value={}), patch.object(graph.time, "sleep"), \
                patch.object(graph.incidents, "record"):
            state = runner.diagnose(evidence, {}, "test", {}, "all", recorded)
        self.assertEqual(recorded, followups)
        second_inputs = model.call_args.args[0]
        self.assertEqual(json.loads(second_inputs["evidence"][0]["detail"]), [initial])
        self.assertEqual(json.loads(second_inputs["evidence"][-1]["detail"]), followups)
        self.assertEqual(detection.ground_truth([initial]), "incident")
        self.assertEqual(detection.ground_truth(recorded), "normal")
        self.assertEqual(state["recheck"]["status"], "resolved")
        payload = graph._context(second_inputs)
        self.assertIn("2506.76", payload)
        for hidden in ("reference", "target_tier", "trial_seed"):
            self.assertNotIn(hidden, payload)

    def test_replay_all_four_share_scored_window_not_separate_client(self):
        end = datetime(2026, 11, 24, 6, tzinfo=timezone.utc)
        prediction = dict(service_detection="incident", impact="degraded", route="handoff",
                          proposal={"action": "manual"}, summary="test", decision_reason="test",
                          root_cause="test", manual_steps=[])
        for name in control_replay.NAMES:
            case = control_replay.plan_case(42, name)
            window = control_replay.make_window(case, "test", "test-cluster", end)
            before = batch(9999, at=window["start"])
            active = batch(2506.76, at=(end - timedelta(seconds=2)).isoformat())
            logs = [json.dumps(dict(s, client={"at": s["at"], "code": 200, "ms": 1})) for s in (before, active)]
            row = {"incident_id": "test", "target_tier": "SECRET", "reference": {"route": "SECRET"}}
            with patch.dict(eks_replay.os.environ, KUBEOPS_EKS_CREATED_AT="test-cluster"), \
                    patch.object(eks_replay, "datetime") as clock, \
                    patch.object(eks_replay, "endpoint_sample", return_value=None), \
                    patch.object(eks_replay, "observer_logs", return_value=logs), \
                    patch.object(graph, "retrieve", return_value={"docs": []}), \
                    patch.object(graph, "analyze", return_value={"analysis": prediction}) as model:
                clock.now.return_value = end
                eks_replay.evaluate_replay(case, row, False)
            sent = model.call_args.args[0]
            self.assertEqual(json.loads(sent["evidence"][1]["detail"]), row["probes"])
            self.assertEqual(len(row["probes"]), 1)
            self.assertEqual(row["probes"][0]["p95_ms"], 2506.76)
            self.assertEqual(row["truth"], detection.ground_truth(row["probes"]))
            payload = graph._context(sent)
            for hidden in ("SECRET", "reference", "target_tier", "trial_seed", name, "9999"):
                self.assertNotIn(hidden, payload)

    def test_recovered_app_still_reaches_diagnosis_in_single_run(self):
        sample = dict(api_code=200, api_ms=12, echo_ok=True, endpoints=2, ready=2, desired=2,
                      rps=5, p95_ms=12, error_ratio=0, last_pod_warning=0)
        case = {"case": "normal", "conditions": dict(visibility="all", onset_s=0, duration_s=0, repeat=1, recovery="observe")}
        row = {"incident_id": "test"}
        def echo(row, evidence, url):
            row["app_checks"].append({"outcome": "recovered"})
        def diagnose(row, *args):
            self.assertTrue(row["app_recovered_before_diagnosis"])
            self.assertEqual(row["truth"], "normal")
            row["prediction_status"] = "complete"
        with patch.multiple(runner, EKS_LAB=False, prepare=Mock(), rollout=Mock(),
                            wait_sample=Mock(return_value=sample), node_capacity=Mock(return_value={}),
                            resource=Mock(return_value={"spec": {"selector": {"app": "api"}}}),
                            inject=Mock(return_value=nullcontext({})), establish=Mock(return_value=sample),
                            oracle_probe=Mock(return_value=batch()), model_evidence=Mock(return_value=[]),
                            interval_evidence=Mock(return_value=[]), app_check=echo), \
                patch.object(runner.time, "sleep"), patch.object(runner.incidents, "record"), \
                patch.object(runner, "assess_and_act", side_effect=diagnose) as model:
            runner.evaluate(case, row, False, None, False)
        model.assert_called_once()
        self.assertEqual(row["prediction_status"], "complete")

    def test_legacy_reports_are_read_without_rewriting_and_cannot_mix_on_resume(self):
        doc = dict(protocol=detection.PROTOCOL, cases=[{"case": "normal"}], model="old", started="old",
                   seed=42, fixtures_only=False, campaign_mix={"healthy": 35})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "detection-old.json"
            path.write_text(json.dumps(doc))
            original = path.read_bytes()
            rows = detection.report_rows(path.parent)
            self.assertEqual(rows[0]["http_observation_version"], "separate-http-v1")
            with self.assertRaisesRegex(ValueError, "HTTP 관측 버전"):
                runner.resume_campaign(path, {"http_observation_version": HTTP_OBSERVATION_VERSION})
            self.assertEqual(original, path.read_bytes())
            new = deepcopy(doc)
            new["http_observation_version"] = HTTP_OBSERVATION_VERSION
            (path.parent / "detection-new.json").write_text(json.dumps(new))
            self.assertEqual({r["http_observation_version"] for r in detection.report_rows(path.parent)},
                             {HTTP_OBSERVATION_VERSION, "separate-http-v1"})
