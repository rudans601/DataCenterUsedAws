"""Kubernetes exec 인증 플러그인. 에이전트에는 demo 전용 토큰만 전달한다."""
import json
import os
from pathlib import Path
import subprocess

admin = Path(__file__).resolve().parents[1] / ".kube/eks-admin.kubeconfig"
kubectl = ["kubectl", "--kubeconfig", str(admin), "--context", "datacenterusedaws-lab"]
endpoint = subprocess.check_output(kubectl + [
    "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"
], text=True)
if not endpoint or endpoint != os.environ.get("KUBEOPS_EKS_VERIFIED_ENDPOINT"):
    raise SystemExit("EKS 엔드포인트를 다시 확인하세요.")
token = json.loads(subprocess.check_output(kubectl + [
    "-n", "kubeops", "create", "token", "kubeops-copilot", "--duration=1h", "-o", "json"
]))["status"]
print(json.dumps({"apiVersion": "client.authentication.k8s.io/v1", "kind": "ExecCredential",
                  "status": {"token": token["token"], "expirationTimestamp": token["expirationTimestamp"]}}))
