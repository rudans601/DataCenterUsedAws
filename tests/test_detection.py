import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import single_detection as runner
from kubeops_copilot import detection as d
from kubeops_copilot.scenarios import NAMES, condition_rows, single_plan


class DetectionTests(unittest.TestCase):
    def test_balanced_seeded_valid_conditions(self):
        for seed in range(50):
            plan = single_plan(seed)
            self.assertEqual(plan, single_plan(seed))
            self.assertEqual(len(plan), 14)
            self.assertEqual({c['case'] for c in plan}, set(NAMES))
            for case in plan:
                c = case['conditions']
                self.assertEqual(len(condition_rows(case)), 9)
                self.assertNotIn("secondary", case)
                if c['repeat'] > 1:
                    self.assertIn(case['case'], ('high-load', 'transient-warning'))
                if case['case'] == 'maintenance':
                    self.assertGreaterEqual(c['replicas'], 2)
                if c['background'] == 'healthy_rollout':
                    self.assertEqual(c['onset_s'], 30)

    def test_truth_uses_independent_pre_action_http(self):
        healthy = dict(failed=0, count=4, p95_ms=300)
        self.assertEqual(d.ground_truth([healthy]), 'normal')
        for issue in (dict(healthy, p95_ms=301), dict(healthy, failed=1)):
            self.assertEqual(d.ground_truth([healthy, issue]), 'incident')
        self.assertEqual(d.ground_truth([dict(healthy, failed=4, p95_ms=None)]), "incident")
        self.assertEqual(d.ground_truth([]), 'unknown')
        self.assertEqual(d.ground_truth([dict(healthy, count=1)]), 'unknown')

    def test_abstention_timeouts_and_no_fake_negatives(self):
        base = dict(fixtures_only=False, measurement_status='complete', truth='incident')
        rows = [dict(base, truth=t, prediction=p) for t,p in d.CELLS.values()]
        rows += [dict(base, prediction='unknown'), dict(base, prediction_status='timeout'),
                 dict(base, fixtures_only=True), dict(base, measurement_status='timeout')]
        counts, m = d.statistics(rows)
        self.assertEqual([counts[k] for k in d.CELLS], [1]*4)
        self.assertEqual(m['미탐률'], .5)
        self.assertEqual(m['판정 완료율'], 4/7)
        self.assertIsNone(d.statistics([])[1]['재현율'])
        for name in ('판단 보류','판정 시간 초과','실험 시간 초과','실측 전용'):
            self.assertEqual(counts[name],1)

    def test_single_verdict_even_after_recovery_and_repeats(self):
        with tempfile.TemporaryDirectory() as out:
            doc = {"protocol":d.PROTOCOL,"started":"today","seed":7,"model":"test","fixtures_only":False,
                   "cases":[{"case":"high-load","prediction":"normal","truth":"incident","measurement_status":"complete",
                             "episodes":[1,2,3],"after_fixture_cleanup":{"api_code":200}}]}
            folder = Path(out)
            (folder/'detection-real.json').write_text(json.dumps(doc))
            (folder/'mentoring-old.json').write_text(json.dumps(doc))
            (folder/'detection-other.json').write_text(json.dumps(dict(doc,protocol='other')))
            rows = d.report_rows(folder)
            self.assertEqual(len(rows),1)
            self.assertEqual(d.bucket(rows[0]),'FN')

    def test_raw_truth_hidden_and_resource_readings_visible(self):
        def collect(ns, targets, book):
            book.add("logs","api","로그","LOG")
            book.add("metrics","api","메트릭","REQUEST_DATA")
            return {"metrics":{"api":{"cpu_util":.5,"rps":991}}}
        window = {"api_code":200, "api_ms":5, "p95_ms":999, "truth":"incident", "rps":991}
        with patch.object(runner.k8s_tools,'collect_evidence',side_effect=collect):
            for visibility,hidden in [('no_request_metrics','REQUEST_DATA'),('no_app_logs','LOG')]:
                rows = runner.model_evidence(visibility, window)
                text = json.dumps(rows)
                self.assertNotIn(hidden,text)
                self.assertNotIn('"truth"',text)
                if visibility == 'no_request_metrics':
                    self.assertNotIn('991',text)
                    self.assertIn('cpu_util',text)

    def test_repeated_step_load_is_one_case_with_overlapping_clients(self):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        case = next(c for c in single_plan(1) if c["case"] == "high-load")
        case["conditions"].update(pattern="step", duration_s=60, onset_s=0, repeat=3, gap_s=10)
        clients, observed_load = [], []
        @contextmanager
        def pulse(qps, seconds, records):
            clients.append(qps)
            try:
                yield 10000000000
            finally:
                clients.pop()
        def measured(case, row, evidence, end, sample):
            observed_load.append(sum(clients))
            row["probes"].append(dict(count=4, failed=0, p95_ms=6))
        row = {}
        with patch.multiple(runner, prepare=MagicMock(), rollout=MagicMock(), wait_sample=MagicMock(return_value={}),
                            node_capacity=MagicMock(), resource=MagicMock(return_value={"spec":{"selector":{"app":"api"}}}),
                            observe=MagicMock(return_value={}), traffic_pulse=pulse, measure=measured), \
             patch.object(runner.time, "sleep"):
            runner.evaluate(case, row, True, None, False)
        self.assertEqual(observed_load, [case["qps"]//2, case["qps"]] * 3)
        self.assertEqual(row["truth"], "normal")
        self.assertEqual(row["recovered_by"], "load_ends")
        self.assertEqual(len(row["episodes"]), 3)
        self.assertNotIn("prediction",row)

    def test_load_streams_are_separate_and_pod_is_cleaned_up(self):
        from unittest.mock import MagicMock
        image = {"spec": {"template": {"spec": {"containers": [{"image": "fortio"}]}}}}
        for code in (0, 137):
            def start(cmd, *, cwd, stdout, stderr):
                json.dump({"DurationHistogram": {"Count": 42}, "RetCodes": {"200": 42}}, stdout)
                stderr.write("diagnostic output")
                proc = MagicMock()
                proc.__enter__.return_value = proc
                proc.wait.return_value = code
                return proc
            with self.subTest(code=code), patch.object(runner, "resource", return_value=image), \
                    patch.object(runner, "kubectl") as cli, patch.object(runner.subprocess, "Popen", side_effect=start):
                records = []
                if code:
                    with self.assertRaisesRegex(RuntimeError, "137.*diagnostic output"):
                        with runner.traffic_pulse(600, 10, records):
                            pass
                else:
                    with runner.traffic_pulse(600, 10, records):
                        pass
                    self.assertEqual(records[0]["sent"], 42)
                self.assertEqual(cli.call_args.args[0], "delete")

    def test_cli_approval_only_applies_to_specified_rollback(self):
        p = {"action": "rollback", "namespace": "demo", "deployment": "api"}
        state = {"guard": {"normalized": p}}
        self.assertFalse(runner.operator_decision(state, None, None)["approved"])
        self.assertTrue(runner.operator_decision(state, "운영자", None)["approved"])
        for change in ({"action": "restart"}, {"namespace": "other"}, {"deployment": "web"}):
            self.assertFalse(runner.operator_decision({"guard": {"normalized": {**p, **change}}}, "운영자", None)["approved"])

    def test_unscheduled_pod_can_have_null_logs(self):
        with patch.object(runner.k8s_tools,"core") as core:
            core.return_value.read_namespaced_pod_log.return_value = None
            self.assertEqual(runner.k8s_tools.pod_logs("demo","pending-pod"), "(로그 없음)")
