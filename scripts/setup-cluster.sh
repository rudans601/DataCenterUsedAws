#!/usr/bin/env bash
# 로컬 테스트 클러스터를 처음부터 구성한다.
#   kind 클러스터 -> Prometheus -> 데모 앱 -> 부하 생성기 -> Copilot 전용 권한
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER=datacenterusedaws
CTX="kind-$CLUSTER"
export KUBECONFIG="$ROOT/.kube/admin.kubeconfig"
umask 077
mkdir -p "$ROOT/.kube"

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "==> kind 클러스터 생성"
  kind create cluster --config "$ROOT/k8s/kind-cluster.yaml" --kubeconfig "$KUBECONFIG" --wait 180s
elif [[ ! -s "$KUBECONFIG" ]]; then
  kind export kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG"
fi
# 데모 이미지(podinfo, fortio)는 각 노드가 레지스트리에서 직접 받는다.

echo "==> Prometheus 설치"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
helm upgrade --install prometheus prometheus-community/prometheus \
  --kube-context "$CTX" -n monitoring --create-namespace \
  -f "$ROOT/k8s/monitoring/prometheus-values.yaml" --wait --timeout 6m

echo "==> 데모 앱, 부하 생성기, 권한 적용"
kubectl --context "$CTX" apply -f "$ROOT/k8s/demo/namespace.yaml"
kubectl --context "$CTX" apply -f "$ROOT/k8s/demo/" -f "$ROOT/k8s/traffic/" -f "$ROOT/k8s/rbac/"
kubectl --context "$CTX" -n demo rollout status deploy/api --timeout=180s
kubectl --context "$CTX" -n demo rollout status deploy/web --timeout=180s

"$ROOT/scripts/make-agent-kubeconfig.sh"

echo
echo "완료"
echo "  web 데모      : http://localhost:8080"
echo "  Prometheus    : http://localhost:9090"
echo "  Copilot 권한  : $ROOT/.kube/agent.kubeconfig"
