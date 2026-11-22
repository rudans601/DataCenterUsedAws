import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import eks_inventory, web_target
from kubeops_copilot.detection import PROTOCOL, report_rows


class EksWebTests(unittest.TestCase):
    def test_default_web_overrides_local_sources(self):
        with patch.dict(os.environ, {"KUBEOPS_LOCAL_LAB": "1", "PROMETHEUS_URL": "http://localhost:9090",
                                     "KUBEOPS_GRAFANA_URL": "http://localhost:3000"}, clear=True):
            self.assertTrue(web_target.configure())
            self.assertEqual(os.environ["KUBEOPS_LOCAL_LAB"], "0")
            self.assertTrue(os.environ["KUBEOPS_DB"].endswith("/data/eks-kubeops.db"))
            self.assertTrue(os.environ["KUBEOPS_KUBECONFIG"].endswith("/.kube/eks-agent.kubeconfig"))
            self.assertEqual(os.environ["PROMETHEUS_URL"], "http://127.0.0.1:19090")
            self.assertFalse(os.environ["KUBEOPS_GRAFANA_URL"])

    def test_eks_history_excludes_old_kind_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report = {"protocol": PROTOCOL, "cases": [{}], "seed": 42, "started": "", "model": "", "fixtures_only": True}
            for name, metadata in [("legacy", {}), ("local", {"environment": "kind"}), ("cloud", {"environment": "eks"})]:
                (directory / f"detection-{name}.json").write_text(json.dumps({**report, **metadata}))
            self.assertEqual([r["report"] for r in report_rows(directory, environment="eks")], ["detection-cloud.json"])

    def test_inventory_restricts_instances_to_verified_cluster_and_group(self):
        endpoint = "https://example.invalid"
        cluster = {"arn": "arn:aws:eks:ap-northeast-2:123:cluster/datacenterusedaws-lab", "endpoint": endpoint,
                   "version": "1.37", "status": "ACTIVE", "tags": {"Project": "DataCenterUsedAws", "Purpose": "scenario-lab"},
                   "resourcesVpcConfig": {"vpcId": "vpc-test"}}
        group = {"status": "ACTIVE", "scalingConfig": {"desiredSize": 2},
                 "resources": {"autoScalingGroups": [{"name": "workers-asg"}]}}
        with patch.dict(os.environ, {"KUBEOPS_AWS_ACCOUNT": "123", "KUBEOPS_EKS_VERIFIED_ENDPOINT": endpoint}):
            with patch.object(eks_inventory, "aws", side_effect=[{"cluster": cluster}, {"nodegroup": group}, {"Reservations": []}]) as aws:
                self.assertEqual(eks_inventory.inventory()["workers"], [])
                filters = aws.call_args.args
                self.assertIn("Name=vpc-id,Values=vpc-test", filters)
                self.assertIn("Name=tag:aws:autoscaling:groupName,Values=workers-asg", filters)
            with patch.object(eks_inventory, "aws", return_value={"cluster": {**cluster, "endpoint": "https://other.invalid"}}) as aws:
                with self.assertRaises(ValueError):
                    eks_inventory.inventory()
                self.assertEqual(aws.call_count, 1)
