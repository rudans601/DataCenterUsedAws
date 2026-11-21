#!/usr/bin/env bash
# 로컬 테스트 클러스터를 삭제한다. (Azure 리소스는 건드리지 않음)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
kind delete cluster --name datacenterusedaws --kubeconfig "$ROOT/.kube/admin.kubeconfig"
rm -f "$ROOT/.kube/agent.kubeconfig" "$ROOT/.kube/admin.kubeconfig"
