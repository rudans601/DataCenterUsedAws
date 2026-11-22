"""웹의 기본 데이터 소스를 AWS 전용 실습 환경으로 설정한다."""
import os
from pathlib import Path


def configure():
    if os.getenv("KUBEOPS_WEB_TARGET") == "kind" and os.getenv("KUBEOPS_EKS_LAB") != "1":
        return False
    root = Path(__file__).resolve().parents[1]
    os.environ.update(
        KUBEOPS_KUBECONFIG=str(root / ".kube/eks-agent.kubeconfig"),
        KUBEOPS_EKS_LAB="1", KUBEOPS_LOCAL_LAB="0", KUBEOPS_BILLABLE_CAPACITY="1",
        KUBEOPS_NAMESPACE="demo", KUBEOPS_DB=str(root / "data/eks-kubeops.db"),
        PROMETHEUS_URL="http://127.0.0.1:19090", KUBEOPS_ECHO_URL="http://127.0.0.1:18080/api/echo",
        KUBEOPS_GRAFANA_URL="http://127.0.0.1:13000" if os.getenv("KUBEOPS_EKS_VERIFIED_ENDPOINT") else "", KUBEOPS_DEMO_API_PROBE_URL="",
    )
    return True
