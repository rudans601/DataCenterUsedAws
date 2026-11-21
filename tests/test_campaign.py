import io
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from eval import single_detection as runner
from kubeops_copilot import audit, detection, graph, incidents
from kubeops_copilot.campaign import DEFAULT_MIX, campaign_plan
from kubeops_copilot.scenarios import single_plan


class CampaignTests(unittest.TestCase):
    def test_exact_reproducible_mix_without_repeated_seeds(self):
        for seed, mix in [(42, DEFAULT_MIX), (71, dict.fromkeys(DEFAULT_MIX, 25)),
                          (42, dict(DEFAULT_MIX, healthy=0, warning=0, degraded=0, critical=100))]:
            plan = campaign_plan(seed, mix)
            self.assertEqual(plan, campaign_plan(seed, mix))
            self.assertEqual(Counter(c["target_tier"] for c in plan), Counter(mix))
            self.assertEqual(len({c["trial_seed"] for c in plan}), 100)
            for c in plan:
                original = next(p for p in single_plan(c["trial_seed"]) if p["case"] == c["case"])
                self.assertEqual(original, {k:v for k,v in c.items() if k not in ("trial_seed", "target_tier", "trial")})
        self.assertNotEqual(campaign_plan(42), campaign_plan(43))
        with self.assertRaises(ValueError):
            campaign_plan(42, dict(DEFAULT_MIX, healthy=0))

    def test_runner_persists_one_row_per_trial_and_can_stop_between_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "eval").mkdir()
            (root / ".kube").mkdir()
            stop = root / "stop"
            def measured(case, row, *args):
                row.update(measurement_status="complete", truth="normal")
                if stop_after and case["trial"] == 2:
                    stop.touch()
            client = SimpleNamespace(configuration=SimpleNamespace(host="https://localhost"))
            with patch.multiple(runner, ROOT=root, kubectl=Mock(return_value=client.configuration.host),
                                rollout=Mock(), wait_sample=Mock(return_value={}), evaluate=measured), patch.object(
                                    runner.k8s_tools, "api_client", return_value=client), redirect_stdout(io.StringIO()):
                for stop_after in (False, True):
                    self.assertEqual(runner.run(123, "campaign", True, None, False, stop_file=stop), int(stop_after))
            rows = detection.report_rows(root / "eval" / "results")
            self.assertEqual(len(rows), 102)
            self.assertTrue(all(r["seed"] == r["trial_seed"] and r["campaign_seed"] == 123 for r in rows))
            self.assertEqual(len({r["incident_id"] for r in rows}), 102)

    def test_metrics_use_measured_impact_and_matching_issued_ticket(self):
        base = dict(fixtures_only=False, measurement_status="complete", prediction_status="complete",
                    target_tier="critical", tier_criteria="current-http-v1", incident_id="abc",
                    injected=dict(ready=2, desired=2, api_code=200, endpoints=2, p95_ms=900, error_ratio=0, rps=3),
                    probes=[dict(count=4, failed=0, p95_ms=15)], truth="normal")
        ticket = dict(kind="사람 인계", id=9, incident="abc")
        rows = [dict(base, prediction="incident"), dict(base, prediction="normal"),
                *[dict(base, prediction="unknown", tickets=t) for t in (
                    [ticket, ticket], [dict(ticket, kind="승인 요청")], [dict(ticket, incident="other")], [])],
                dict(base, prediction="unknown"), dict(base, fixtures_only=True, prediction="unknown")]
        size, counts, rates, h = detection.severity_statistics(rows)["healthy"]
        self.assertEqual((size, counts["TN"], counts["FP"]), (7,1,1))
        self.assertEqual(rates["오탐률"], .5)
        self.assertIsNone(rates["미탐률"])
        self.assertEqual((h["ticketed"], h["held"], h["untracked"]), (1,5,1))
        failed = dict(base, truth="incident", prediction="normal", probes=[dict(count=4, failed=1, p95_ms=15)])
        self.assertEqual(detection.severity_statistics([failed])["degraded"][2]["미탐률"], 1)

    def test_prometheus_queries_stay_inside_their_own_windows(self):
        times = {"injection_started": "1970-01-01T00:02:00.5+00:00", "measured_until": "1970-01-01T00:03:01.9+00:00"}
        with patch.object(runner.metrics, "scalar", return_value=None) as read:
            evidence = runner.interval_evidence(times)
        self.assertEqual([e["source"] for e in evidence], ["history", "metrics"])
        for i, call in enumerate(read.call_args_list):
            self.assertIn("[60s] @ " + ("120" if i < 3 else "181"), call.args[0])
        self.assertEqual(read.call_count, 6)
        self.assertIn('"p95_ms": null', evidence[0]["detail"])

    def test_bounded_recheck_and_ticket_even_if_cluster_is_unavailable(self):
        p = dict(action="scale", namespace="demo", deployment="api", replicas=3)
        a = dict(service_detection="unknown", impact="unknown", confidence="low", route="auto", proposal=p)
        state = dict(thread_id="test", analysis=a, proposal=p, evidence=[{"id":"E1","title":"첫 관측"}], snapshot={})
        with tempfile.TemporaryDirectory() as temp, patch.object(audit, "settings", SimpleNamespace(db_path=Path(temp)/"test.db")), patch.object(
                incidents, "notify", return_value="알림함 저장"), patch.object(graph.time, "sleep"), patch.object(
                graph, "retrieve", return_value={"docs":[]}), patch.object(graph.actions, "check") as check:
            for status in ("held", "resolved", "error"):
                final = a if status == "held" else dict(a, service_detection="normal", impact="healthy", confidence="high")
                reader = Mock(return_value={"evidence":[{"title":"새 관측"}], "snapshot":{}})
                if status == "error":
                    reader.side_effect = TimeoutError()
                state["thread_id"] = status
                with patch.object(graph, "analyze", return_value={"analysis":final, "proposal":p}) as analyze:
                    result = graph.remeasure_once(state, reader)
                    reader.assert_called_once()
                    self.assertEqual(analyze.call_count, int(status != "error"))
                self.assertEqual(result["recheck"]["status"], status)
                if status != "error":
                    self.assertEqual([e["id"] for e in result["evidence"]], ["E1","E2"])
                if status != "resolved":
                    self.assertEqual(result["analysis"]["service_detection"], "unknown")
                    decision = graph.guard({**state, **result})["triage"]
                    self.assertEqual(decision["route"], "handoff")
                    self.assertEqual(incidents.tickets(status)[0]["id"], decision["ticket_id"])
            check.assert_not_called()
            definite = dict(state, analysis=final)
            self.assertEqual(graph.remeasure_once(definite, Mock(side_effect=AssertionError)), {"recheck":{}})
