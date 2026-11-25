#!/usr/bin/env bash
# EKS 관리자 작업은 명시한 계정과 전용 클러스터에 한정한다.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
: "${AWS_PROFILE:?AWS CLI 프로필을 지정하세요}"
: "${KUBEOPS_AWS_ACCOUNT:?예상 AWS 계정 ID를 지정하세요}"
export AWS_REGION=ap-northeast-2 AWS_DEFAULT_REGION=ap-northeast-2 AWS_PAGER=""
cluster=datacenterusedaws-lab
test "$(aws sts get-caller-identity --query Account --output text)" = "$KUBEOPS_AWS_ACCOUNT" || { echo "계정 불일치" >&2; exit 1; }
mkdir -p .kube
export KUBECONFIG="$PWD/.kube/eks-admin.kubeconfig"
py="$PWD/.venv/bin/python"
case "${1:-}" in
  create)
    # 전용 VPC와 고정 수량 노드를 만들고 공개 API는 실행자의 현재 IPv4로 제한한다.
    cidr=$(curl -4fsS --max-time 10 https://checkip.amazonaws.com)
    "$py" - "$cidr" <<'PY'
from ipaddress import IPv4Address
from pathlib import Path
import sys
import yaml
config = yaml.safe_load(Path("k8s/eks/cluster.yaml").read_text())
config["vpc"]["publicAccessCIDRs"] = [str(IPv4Address(sys.argv[1].strip())) + "/32"]
Path(".kube/eks-cluster.yaml").write_text(yaml.safe_dump(config))
PY
    exec eksctl create cluster -f .kube/eks-cluster.yaml --write-kubeconfig=false
    ;;
  setup|observer|run|web|delete|retest) ;;
  *) echo "사용법: $0 create|setup|observer|run [first-three|all|replays|유형] [시드]|retest 보고서 회차|web|delete" >&2; exit 2 ;;
esac
metadata=$(aws eks describe-cluster --name "$cluster" --query 'cluster.[arn,endpoint,tags.Project,tags.Purpose,createdAt]' --output text)
read -r arn endpoint project purpose created_at <<< "$metadata"
[[ "$arn" == "arn:aws:eks:$AWS_REGION:$KUBEOPS_AWS_ACCOUNT:cluster/$cluster" && "$project" == DataCenterUsedAws && "$purpose" == scenario-lab ]] || { echo "실습 클러스터 식별 실패" >&2; exit 1; }
if [[ "$1" == delete ]]; then
    eksctl delete cluster --name "$cluster" --region "$AWS_REGION" --wait
    rm -f .kube/eks-agent.kubeconfig .kube/eks-admin.kubeconfig
    exit 0
fi
aws eks update-kubeconfig --name "$cluster" --alias "$cluster" --kubeconfig "$KUBECONFIG" >/dev/null
k=(kubectl --kubeconfig "$KUBECONFIG" --context "$cluster")
if [[ "$1" == setup || "$1" == observer ]]; then
    "${k[@]}" apply -f k8s/demo/namespace.yaml
    "${k[@]}" -n traffic create configmap http-observer --from-file=probe.py=k8s/observer/probe.py \
        --from-literal=cluster="$created_at" --dry-run=client -o yaml | "${k[@]}" apply -f -
    "${k[@]}" apply -f k8s/observer/deployment.yaml
    # 새 스크립트나 클러스터 식별자가 적용된 경우에만 관측 Pod를 교체한다.
    revision=$(shasum -a 256 k8s/observer/probe.py | cut -d' ' -f1)
    "${k[@]}" -n traffic patch deploy/http-observer --type=merge \
        -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"observer-revision\":\"$revision\"}}}}}"
    "${k[@]}" -n traffic rollout status deploy/http-observer --timeout=180s
    if [[ "$1" == observer ]]; then exit 0; fi
fi
if [[ "$1" == setup ]]; then
    "${k[@]}" apply -f k8s/demo/api.yaml -f k8s/traffic/loadgen.yaml -f k8s/rbac/agent-rbac.yaml
    "$py" - <<'PY' | "${k[@]}" apply -f -
from pathlib import Path
import yaml
objects = list(yaml.safe_load_all(Path("k8s/demo/web.yaml").read_text()))
for obj in objects:
    if obj["kind"] == "Service":
        obj["spec"]["type"] = "ClusterIP"
        obj["spec"]["ports"][0].pop("nodePort", None)
print(yaml.safe_dump_all(objects))
PY
    helm upgrade --install prometheus prometheus --repo https://prometheus-community.github.io/helm-charts \
        --version 29.47.1 --kube-context "$cluster" -n monitoring --create-namespace \
        -f k8s/monitoring/prometheus-values.yaml --set server.service.type=ClusterIP --wait --timeout 10m
    "${k[@]}" -n monitoring create configmap kubeops-grafana-dashboard \
        --from-file=demo-api.json=k8s/monitoring/demo-api.json --dry-run=client -o yaml | "${k[@]}" apply -f -
    helm upgrade --install grafana grafana --repo https://grafana-community.github.io/helm-charts \
        --version 14.5.0 --kube-context "$cluster" -n monitoring -f k8s/monitoring/grafana-values.yaml --wait --timeout 10m
    "${k[@]}" -n demo rollout status deployment/api --timeout=180s
    "${k[@]}" -n demo rollout status deployment/web --timeout=180s
    exit 0
fi

export KUBEOPS_KUBECONFIG="$PWD/.kube/eks-agent.kubeconfig"
export KUBEOPS_EKS_LAB=1 KUBEOPS_LOCAL_LAB=0 KUBEOPS_BILLABLE_CAPACITY=1 KUBEOPS_NAMESPACE=demo
export KUBEOPS_EKS_VERIFIED_ENDPOINT="$endpoint" KUBEOPS_DB="$PWD/data/eks-kubeops.db"
export KUBEOPS_ECHO_URL=http://127.0.0.1:18080/api/echo PROMETHEUS_URL=http://127.0.0.1:19090
export KUBEOPS_DEMO_API_PROBE_URL="" KUBEOPS_GRAFANA_URL="" KUBEOPS_EKS_CREATED_AT="$created_at"
"$py" - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
cmd = ["kubectl", "--kubeconfig", os.environ["KUBECONFIG"], "--context", "datacenterusedaws-lab"]
cfg = json.loads(subprocess.check_output(cmd + ["config", "view", "--minify", "--flatten", "-o", "json", "--raw"]))
if cfg["clusters"][0]["cluster"]["server"] != os.environ["KUBEOPS_EKS_VERIFIED_ENDPOINT"]:
    raise SystemExit("EKS 서버 주소 불일치")
cfg["users"] = [{"name": "agent", "user": {"exec": {
    "apiVersion": "client.authentication.k8s.io/v1", "command": sys.executable,
    "args": [str(Path.cwd() / "scripts/eks-agent-token.py")], "interactiveMode": "Never",
}}}]
cfg["contexts"][0]["context"].update(user="agent", namespace="demo")
path = Path(os.environ["KUBEOPS_KUBECONFIG"])
with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
    json.dump(cfg, f)
os.replace(f.name, path)
PY
pids=()
logs=()
cleanup() {
    for pid in ${pids[@]+"${pids[@]}"}; do kill "$pid" 2>/dev/null || true; done
    for log in ${logs[@]+"${logs[@]}"}; do rm -f "$log"; done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# 워크로드 교체로 연결이 끊겨도 웹 세션은 유지하고 해당 터널만 다시 연다.
serve_tunnel() {
    local namespace=$1 service=$2 ports=$3 log=$4 mode=$5 child="" connected=0
    trap 'if [[ -n "$child" ]]; then kill "$child" 2>/dev/null || true; fi; exit 0' TERM INT
    while true; do
        "${k[@]}" -n "$namespace" port-forward --address 127.0.0.1 "svc/$service" "$ports" >"$log" 2>&1 &
        child=$!
        wait "$child" || true
        child=""
        if grep -q '^Forwarding from' "$log"; then connected=1; fi
        [[ "$mode" == web && "$connected" == 1 ]] || exit 1
        sleep 2
    done
}
forward() {
    local namespace=$1 service=$2 ports=$3 log pid i
    log=$(mktemp .kube/forward.XXXXXX)
    logs+=("$log")
    serve_tunnel "$namespace" "$service" "$ports" "$log" "$mode" &
    pid=$!
    pids+=("$pid")
    for ((i=0; i<100; i++)); do
        kill -0 "$pid" 2>/dev/null || { cat "$log" >&2; return 1; }
        if grep -q '^Forwarding from' "$log"; then return 0; fi
        sleep 0.2
    done
    echo "터널 연결 시간 초과: $service" >&2
    return 1
}
mode=$1
if [[ "$mode" == retest ]]; then
    : "${2:?원본 평가 보고서 경로를 지정하세요}" "${3:?원본 회차 번호를 지정하세요}"
    # 기존 웹 터널과 충돌하지 않는 별도 Prometheus 포트. HTTP 측정은 관측 Pod에서 수행한다.
    export PROMETHEUS_URL=http://127.0.0.1:19091
    forward monitoring prometheus-server 19091:80
    "$py" eval/single_detection.py --retest-report "$2" --trial "$3"
    exit $?
fi
if [[ "$mode" == run ]]; then
    case "${2:-first-three}" in
        replays|api-timeout|scheduler-stall|controller-lag|etcd-quota)
            exec "$py" eval/single_detection.py --case "$2" --seed "${3:-42}" --fixtures-only ;;
    esac
fi
forward demo web 18080:9898
forward monitoring prometheus-server 19090:80
if [[ "$1" == web ]]; then
    forward monitoring grafana 13000:80
    export KUBEOPS_GRAFANA_URL=http://127.0.0.1:13000
    "$py" -m streamlit run app/streamlit_app.py --server.address=127.0.0.1 --server.port=8501 &
    web_pid=$!
    pids+=("$web_pid")
    wait "$web_pid"
else
    case "${2:-first-three}" in
        first-three) cases=(normal probe-fail maintenance) ;;
        all) cases=(batch) ;;
        *) cases=("$2") ;;
    esac
    for scenario in "${cases[@]}"; do
        "$py" eval/single_detection.py --case "$scenario" --seed "${3:-42}" --fixtures-only
    done
fi
