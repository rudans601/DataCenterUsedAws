from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from eval import revision_evidence as history, single_detection as runner
from eval.compare_revision_evidence import HISTORY_TITLE, inputs, variants


def deployment(revision=26, uid="deployment-uid"):
    return {"metadata": {"uid": uid, "generation": 4,
                         "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                     "template": {"metadata": {"labels": {"app": "api"}},
                                  "spec": {"containers": [{"name": "api", "image": "test:1"}]}}},
            "status": {"observedGeneration": 4}}


def replica_set(dep, available=1):
    rs = deepcopy(dep)
    rs["metadata"].update(name="api-old", ownerReferences=[{"kind": "Deployment", "uid": dep["metadata"]["uid"]}])
    rs["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] = "abc"
    rs["status"].update(readyReplicas=available, availableReplicas=available)
    return rs


class RevisionEvidenceTests(unittest.TestCase):
    def test_comparison_changes_only_runbook_and_revision_evidence_and_hides_answers(self):
        evidence = [{"id": "E1", "source": "probe", "target": "demo/api", "severity": "info",
                     "title": "HTTP", "detail": "measured failures"},
                    {"id": "E2", "source": "history", "target": "demo/api", "severity": "info",
                     "title": HISTORY_TITLE, "detail": "observed revision"}]
        row = {"evidence": evidence, "reference": "SECRET", "target_tier": "SECRET", "trial_seed": "SECRET"}
        old = [{"id": "D1", "source": "runbook", "content": "before"}]
        new = [{"id": "D1", "source": "runbook", "content": "after"}]
        a, b, c = variants(row, old, new)
        self.assertEqual(a[1], b[1])
        self.assertEqual(b[2], c[2])
        self.assertEqual([e for e in c[1] if e["title"] != HISTORY_TITLE], a[1])
        self.assertNotIn("SECRET", json.dumps(inputs(c[1], c[2])))
        with self.assertRaises(ValueError):
            variants({"evidence": evidence[:1]}, old, new)

    def setUp(self):
        self.dep = deployment()
        self.rs = replica_set(self.dep)
        self.sample = {"api_observed_at": "2026-11-25T01:00:01+00:00", "api_code": 200, "api_ms": 5,
                       "ready": 1, "desired": 1, "endpoints": 1, "target_tier": "SECRET", "reference": "SECRET"}

    def baseline(self, before=None, after=None, sets=None, sample=None):
        return history.baseline_record(before or self.dep, after or self.dep,
                                       [self.rs] if sets is None else sets, sample or self.sample,
                                       "2026-11-25T01:00:00+00:00", "2026-11-25T01:00:02+00:00")

    def test_scaled_down_previous_set_still_matches_observed_healthy_revision(self):
        baseline = self.baseline()
        self.assertTrue(baseline["matched"])
        current = deployment(27)
        current["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {"httpGet": {"path": "/wrong"}}
        result = history.recovery_evidence(baseline, current, [replica_set(self.dep, 0)], "later")
        payload = json.loads(result["detail"])
        self.assertTrue(payload["previous_template_matches_baseline"])
        self.assertEqual(payload["previous_available_now"], 0)
        self.assertNotIn("SECRET", json.dumps(result))
        for hidden in ("reference", "target_tier", "trial_seed", "approval", "rollback"):
            self.assertNotIn(hidden, result["detail"])

    def test_changed_identity_template_or_stale_http_is_not_certified(self):
        for changed in ("uid", "generation", "template", "time", "http", "owner", "available"):
            before, after, rs, sample = deepcopy(self.dep), deepcopy(self.dep), deepcopy(self.rs), deepcopy(self.sample)
            if changed == "uid": after["metadata"]["uid"] = "another"
            elif changed == "generation": after["metadata"]["generation"] += 1
            elif changed == "template": after["spec"]["template"]["spec"]["containers"][0]["image"] = "other"
            elif changed == "time": sample["api_observed_at"] = "2026-11-25T00:59:59+00:00"
            elif changed == "http": sample["api_code"] = 503
            elif changed == "owner": rs["metadata"]["ownerReferences"][0]["uid"] = "another"
            else: rs["status"]["availableReplicas"] = 0
            with self.subTest(changed=changed):
                self.assertFalse(self.baseline(before, after, [rs], sample)["matched"])

    def test_replaced_owner_changed_template_or_intervening_revision_is_not_candidate(self):
        baseline = self.baseline()
        for changed in ("uid", "template", "intervening", "missing"):
            current, prior = deployment(27), deepcopy(self.rs)
            sets = [prior]
            if changed == "uid": current["metadata"]["uid"] = "new-deployment"
            elif changed == "template": prior["spec"]["template"]["spec"]["containers"][0]["image"] = "other"
            elif changed == "intervening": current = deployment(28); sets.append(replica_set(deployment(27)))
            else: sets = []
            with self.subTest(changed=changed):
                result = history.recovery_evidence(baseline, current, sets, "later")
                self.assertFalse(json.loads(result["detail"])["previous_template_matches_baseline"])

    def test_live_retest_only_runs_selected_trial_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "eval/results").mkdir(parents=True)
            (root / ".kube").mkdir()
            source = root / "eval/results/detection-source.json"
            source.write_text(json.dumps({"environment": "kind", "model": runner.settings.llm_model,
                "campaign_plan": [{"trial": n, "case": "probe-fail", "trial_seed": 100+n,
                                   "conditions": {"duration_s": 180}} for n in range(1, 101)]}))
            original = source.read_bytes()
            def evaluate(case, row, *args):
                self.assertTrue(row["judgement_only"])
                self.assertEqual(case["trial"], 22)
                row["measurement_status"] = "complete"
            client = SimpleNamespace(configuration=SimpleNamespace(host="https://localhost"))
            with patch.multiple(runner, ROOT=root, EKS_LAB=False, kubectl=Mock(return_value="https://localhost"),
                                rollout=Mock(), wait_sample=Mock(return_value={}), evaluate=Mock(side_effect=evaluate)), \
                    patch.object(runner.k8s_tools, "api_client", return_value=client), \
                    patch.object(runner.incidents, "record"), patch.object(runner.incidents, "timeline", return_value=[]), \
                    patch.object(runner.incidents, "tickets", return_value=[]), redirect_stdout(io.StringIO()):
                self.assertEqual(runner.run(42, "batch", False, None, False, retest_report=source, trial=22), 0)
                runner.evaluate.assert_called_once()
            self.assertEqual(source.read_bytes(), original)
            reports = [p for p in source.parent.glob("detection-*.json") if p != source]
            self.assertEqual(len(reports), 1)
            result = json.loads(reports[0].read_text())
            self.assertEqual([r["trial"] for r in result["cases"]], [22])
            self.assertEqual(result["retest"]["trial"], 22)
            self.assertEqual(result["seed"], 122)
