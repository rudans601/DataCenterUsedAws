#!/usr/bin/env bash
# 모든 시나리오를 되돌리고 기준 상태(baseline)로 맞춘다.
source "$(dirname "$0")/_lib.sh"
k apply -f "$ROOT/k8s/demo/" -f "$ROOT/k8s/traffic/" >/dev/null
k -n demo rollout status deploy/api --timeout=180s >/dev/null
k -n demo rollout status deploy/web --timeout=180s >/dev/null
k -n traffic rollout status deploy/loadgen-api --timeout=120s >/dev/null
echo "baseline 복구 완료"
