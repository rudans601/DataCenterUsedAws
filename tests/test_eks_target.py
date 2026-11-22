import unittest
from unittest.mock import patch

from eval import single_detection as runner


class LabTargetTests(unittest.TestCase):
    def test_eks_requires_matching_operator_agent_and_verified_endpoint(self):
        endpoint = "https://lab.example.invalid"
        with patch.object(runner, "EKS_LAB", True), patch.dict(
            runner.os.environ, {"KUBEOPS_EKS_VERIFIED_ENDPOINT": endpoint}
        ):
            runner.validate_target(endpoint, endpoint)
            with self.assertRaises(RuntimeError):
                runner.validate_target(endpoint, "https://localhost")
            with self.assertRaises(RuntimeError):
                runner.validate_target("https://other.invalid", "https://other.invalid")

    def test_kind_rejects_remote_endpoint(self):
        with patch.object(runner, "EKS_LAB", False):
            with self.assertRaises(RuntimeError):
                runner.validate_target("https://remote.invalid", "https://remote.invalid")
