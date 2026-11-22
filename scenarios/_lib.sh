#!/usr/bin/env bash
# 시나리오 스크립트 공통 설정 (관리자 권한 context 사용)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX=kind-datacenterusedaws
CFG="$ROOT/.kube/admin.kubeconfig"
if [[ "${KUBEOPS_EKS_LAB:-0}" == 1 ]]; then
  CTX=datacenterusedaws-lab
  CFG="$ROOT/.kube/eks-admin.kubeconfig"
fi
k() { kubectl --kubeconfig "$CFG" --context "$CTX" "$@"; }
