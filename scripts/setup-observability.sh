#!/usr/bin/env bash
# 기존 kind와 Prometheus에 Grafana 및 수동 동기화 Argo CD를 추가한다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export KUBECONFIG="$ROOT/.kube/admin.kubeconfig"
k=(kubectl --context kind-datacenterusedaws)
h=(helm --kube-context kind-datacenterusedaws)
"${k[@]}" -n monitoring get svc prometheus-server >/dev/null
"${k[@]}" -n demo get deployment api web >/dev/null

"${k[@]}" -n monitoring create configmap kubeops-grafana-dashboard \
  --from-file=demo-api.json=k8s/monitoring/demo-api.json --dry-run=client -o yaml | "${k[@]}" -n monitoring apply -f -

"${h[@]}" upgrade --install grafana grafana --repo https://grafana-community.github.io/helm-charts \
  --version 14.5.0 -n monitoring -f k8s/monitoring/grafana-values.yaml --wait --timeout 18m
"${h[@]}" upgrade --install argocd argo-cd --repo https://argoproj.github.io/argo-helm \
  --version 10.18.1 -n argocd --create-namespace -f k8s/argocd/values.yaml --wait --timeout 18m

"${k[@]}" apply -f k8s/argocd/demo.yaml
"${k[@]}" apply -f k8s/argocd/viewer-rbac.yaml
