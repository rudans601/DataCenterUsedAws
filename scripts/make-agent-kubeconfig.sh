#!/usr/bin/env bash
# 로컬 kind 클러스터에서 에이전트 전용 단기 자격을 발급한다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTX=kind-datacenterusedaws
OUT="$ROOT/.kube/agent.kubeconfig"
admin=(kubectl --kubeconfig "$ROOT/.kube/admin.kubeconfig" --context "$CTX")

SERVER=$("${admin[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$("${admin[@]}" config view --minify --raw --flatten -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
if [[ ! "$SERVER" =~ ^https://(127\.0\.0\.1|localhost|\[::1\]):[0-9]+$ || -z "$CA" ]]; then
  echo "로컬 kind API 주소와 TLS 인증서를 확인할 수 없어요." >&2
  exit 1
fi

umask 077
TOKEN=$("${admin[@]}" -n kubeops create token kubeops-copilot --duration=8h)
[[ -n "$TOKEN" ]]
tmp=$(mktemp "$OUT.XXXXXX")
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: datacenterusedaws
    cluster:
      server: $SERVER
      certificate-authority-data: $CA
users:
  - name: kubeops-copilot
    user:
      token: $TOKEN
contexts:
  - name: kubeops-agent
    context:
      cluster: datacenterusedaws
      user: kubeops-copilot
      namespace: demo
current-context: kubeops-agent
YAML

kubectl --kubeconfig "$tmp" auth can-i list pods -n demo >/dev/null
mv -f "$tmp" "$OUT"
echo "에이전트 kubeconfig를 갱신했어요. 실행 중인 웹 앱은 다시 시작하세요."
