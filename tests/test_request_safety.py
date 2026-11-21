import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from eval import request_safety as runner
from kubeops_copilot import graph, request_safety


def row(expected, actual):
    return {"expected": expected, "actual": actual}


class SafetyGateTests(unittest.TestCase):
    def test_threshold_and_missing_samples(self):
        baseline = [row("block", "block"), *[row("allow", "allow")] * 19, row("allow", "block")]
        score = request_safety.summarize(baseline)
        self.assertEqual((score["recall"], score["false_block_rate"]), (1.0, 0.05))
        self.assertTrue(score["passed"])
        for rows in (baseline[1:], baseline[:1], baseline + [row("allow", "unknown")],
                     baseline + [row("block", "allow")], baseline + [row("allow", "block")]):
            self.assertFalse(request_safety.summarize(rows)["passed"])
        self.assertEqual(request_safety.summarize(baseline + [row("block", "allow")])["leaks"], 1)

    def test_request_scoring_stops_at_guard_and_requires_matching_approval(self):
        proposal = {"action": "scale", "namespace": "demo", "deployment": "api", "replicas": 3}
        case = {"question": "scale", "expected": "allow", "request": proposal}
        for route, expected, target in (("approval", "allow", "api"), ("handoff", "block", "api"),
                                        ("approval", "unknown", "web"), ("auto", "unknown", "api")):
            guard = Mock(return_value={"guard": {"normalized": {**proposal, "deployment": target}},
                                       "triage": {"route": route, "reason": "test"}})
            with patch.multiple(graph, understand=Mock(return_value={}), collect=Mock(return_value={}),
                                retrieve=Mock(return_value={}), analyze=Mock(return_value={}), guard=guard,
                                remeasure_once=Mock(return_value={}), execute=Mock(), approval=Mock()):
                self.assertEqual(runner.check_case(case)["actual"], expected)
                self.assertEqual(guard.call_args.kwargs, {"record_events": False})
                graph.execute.assert_not_called()
                graph.approval.assert_not_called()

    def test_historical_run_does_not_depend_on_detection_results(self):
        import json
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "request-safety-1.json"
            rows = [{"label": "test", "reason": "-", "question": "q", **row(a, b)}
                    for a, b in [("block", "block"), ("allow", "allow")]]
            path.write_text(json.dumps({"started": "2026-11-21T11:00:00", "model": "test",
                                        "prompt_version": "test", "corpus_revision": "test", "rows": rows}))
            code = f"from pathlib import Path\nfrom app.detection_ui import render\nrender(Path({folder!r}))"
            app = AppTest.from_string(code).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.success[0].value, "가드레일 통과")
            self.assertEqual(app.metric[0].value, "1/1 (100.0%)")
            rows[0]["actual"] = "allow"
            doc = json.loads(path.read_text())
            path.write_text(json.dumps({**doc, "rows": rows}))
            self.assertEqual(app.run().error[0].value, "가드레일 미통과")
