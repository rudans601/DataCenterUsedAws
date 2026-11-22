"""프로젝트 전용 클러스터 실습의 제한된 변경과 복구."""
from contextvars import ContextVar
from kubeops_copilot.observations import now
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
EKS_LAB = os.getenv("KUBEOPS_EKS_LAB") == "1"
if EKS_LAB:
    KUBECTL = ("kubectl", "--kubeconfig", str(ROOT / ".kube" / "eks-admin.kubeconfig"),
               "--context", "datacenterusedaws-lab")
else:
    KUBECTL = ("kubectl", "--kubeconfig", str(ROOT / ".kube" / "admin.kubeconfig"),
               "--context", "kind-datacenterusedaws")
TEMPLATE = "/spec/template"
CONTAINER = TEMPLATE + "/spec/containers/0"
ARGS = CONTAINER + "/args"
CAUSE = "/metadata/annotations/kubernetes.io~1change-cause"

SCRIPT_FIELDS = {
    "crashloop": [ARGS, CAUSE], "oom": [ARGS, CAUSE], "bad-deploy": [ARGS, CAUSE],
    "probe-fail": [CONTAINER + "/readinessProbe/httpGet/path", CAUSE],
    "high-load": [ARGS, "/spec/replicas"], "service-broken": ["/spec/selector"],
}


# 평가 스레드에서만 현재 사건의 운영 API 영수증을 모은다.
control_log = ContextVar("control_log", default=None)


def record_control(stage, target, result, **facts):
    log = control_log.get()
    if log is not None:
        log.append({"at": now(), "stage": stage, "target": target, "result": result, **facts})


def kubectl(*args, data=None, namespace="demo"):
    limit = 155 if args[:2] == ("rollout", "status") else 25
    stage = {"get": "read", "patch": "write", "create": "write", "cordon": "write",
             "uncordon": "write", "delete": "write"}.get(args[0])
    target = f"{namespace} · {args[0]} {' '.join(a for a in args[1:3] if not a.startswith('-'))}".strip()
    try:
        proc = subprocess.run([*KUBECTL, f"--request-timeout={limit}s", "-n", namespace, *args],
                              cwd=ROOT, input=data, text=True, capture_output=True, timeout=limit+5)
    except subprocess.TimeoutExpired:
        if stage:
            record_control(stage, target, "timeout")
        raise
    if proc.returncode:
        error = proc.stderr.lower()
        status = "timeout" if any(t in error for t in ("timed out", "timeout", "deadline exceeded")) else "error"
        if stage:
            record_control(stage, target, status)
        raise RuntimeError(f"kubectl {args[0]}: {proc.stderr.strip()[-500:]}")
    if stage:
        record_control(stage, target, "ok")
    return proc.stdout.strip()


def resource(kind, name="api", ns="demo"):
    obj = json.loads(kubectl("get", kind, name, "-o", "json", namespace=ns))
    if control_log.get() is not None and kind in {"deploy", "deployment"} and ns == "demo" and name == "api":
        record_control("controller", "demo/api", "observed", desired=obj["spec"].get("replicas", 1),
                       generation=obj["metadata"].get("generation"), observed_generation=obj.get("status", {}).get("observedGeneration"))
    return obj


def patch(target, ops, ns="demo"):
    kubectl("patch", target, "--type=json", "-p", json.dumps(ops), namespace=ns)


MISSING = object()


def value(obj, path):
    for token in path[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        obj = obj[int(key)] if isinstance(obj, list) else obj.get(key, MISSING)
        if obj is MISSING:
            return MISSING
    return obj


def restore_fields(target, ns, uid, before, installed):
    current = resource(*target.split("/"), ns)
    if current["metadata"]["uid"] != uid:
        raise RuntimeError(f"{target}가 실습 중 다시 생성되어 이전 설정을 적용할 수 없습니다.")
    ops, conflicts = [], []
    for path, old in before.items():
        if value(current, path) == old:
            continue
        actual = value(current, path)
        if path not in installed or actual != installed[path]:
            conflicts.append(path)
            continue
        if actual is not MISSING:
            ops.append({"op": "test", "path": path, "value": actual})
        else:
            parent = path.rsplit("/", 1)[0]
            ops.append({"op": "test", "path": parent, "value": value(current, parent)})
        ops.append({"op": "remove", "path": path} if old is MISSING else {"op": "add", "path": path, "value": old})
    if ops:
        patch(target, [{"op": "test", "path": "/metadata/uid", "value": uid}, *ops], ns)
    if conflicts:
        raise RuntimeError(f"{target} 실습 중 외부 변경을 발견했습니다: {conflicts}")


def save_fields(stack, target, ns, fields):
    obj = resource(*target.split("/"), ns)
    installed = {}
    stack.callback(restore_fields, target, ns, obj["metadata"]["uid"], {p: value(obj, p) for p in fields}, installed)
    return installed


def create_owned(stack, manifest):
    name = kubectl("create", "-f", "-", "-o", "name", data=json.dumps(manifest))
    stack.callback(kubectl, "delete", name, "--ignore-not-found", "--wait=false")
    return name.split("/", 1)[1]


def api_pods():
    items = json.loads(kubectl("get", "pod", "-l", "app=api", "-o", "json"))["items"]
    active = [p for p in items if not p["metadata"].get("deletionTimestamp")]
    record_control("placement", "demo/api", "observed", pods=[
        {"uid": p["metadata"]["uid"], "node": p["spec"].get("nodeName")} for p in active
    ])
    return active


@contextmanager
def inject(case):
    name = case["case"]
    uid = uuid.uuid4().hex[:10]
    details = {}
    with ExitStack() as stack:
        if name in SCRIPT_FIELDS or name == "normal":
            target, ns = "deploy/api", "demo"
            if name == "high-load":
                target, ns = "deploy/loadgen-api", "traffic"
            if name == "service-broken":
                target = "svc/api"
            if name != "normal":
                installed = save_fields(stack, target, ns, SCRIPT_FIELDS[name])
            env = {**os.environ, "KUBEOPS_DELAY_PROFILE": case.get("delay_profile", "standard"),
                   "KUBEOPS_LAB_QPS": str(case.get("qps", 2000))}
            try:
                subprocess.run(["bash", str(ROOT / "scenarios" / name / "inject.sh")], env=env, check=True, timeout=60)
            except subprocess.SubprocessError as exc:
                if name != "normal":
                    record_control("write", f"{ns}/{target}", "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "error")
                raise
            if name != "normal":
                record_control("write", f"{ns}/{target}", "ok")
                applied = resource(*target.split("/"), ns)
                installed.update({p: value(applied, p) for p in SCRIPT_FIELDS[name]})
        elif name == "transient-warning":
            from eval.echo_fault import one_unavailable_response
            details["echo_url"] = stack.enter_context(one_unavailable_response())
            create_owned(stack, {"apiVersion": "v1", "kind": "Event", "metadata": {"name": "api-timeout-" + uid},
                "type": "Warning", "reason": "UpstreamTimeout", "message": f"Upstream request timed out after {case['timeout_ms']}ms",
                "involvedObject": {"apiVersion": "apps/v1", "kind": "Deployment", "namespace": "demo", "name": "api",
                                   "uid": resource("deploy")["metadata"]["uid"]}, "source": {"component": "kubeops-scenario"}, "count": 1})
        elif name == "dns":
            # 독립된 테스트 클라이언트의 잘못된 주소. CoreDNS와 운영 앱 설정은 건드릴 필요가 없다.
            details["hostname"] = (f"api.missing-{uid}.svc.cluster.local" if case.get("bad_namespace")
                                   else f"api-{uid}.demo.svc.cluster.local")
        elif name == "maintenance":
            selector = "eks.amazonaws.com/nodegroup=scenario-workers" if EKS_LAB else "!node-role.kubernetes.io/control-plane"
            nodes = json.loads(kubectl("get", "nodes", "-l", selector, "-o", "json"))["items"]
            workers = {n["metadata"]["name"] for n in nodes if not n["spec"].get("unschedulable")}
            pods = [p for p in api_pods() if p["spec"].get("nodeName") in workers and
                    any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))]
            if len(workers) < 2 or len(pods) < 2:
                raise RuntimeError("점검 실습에는 스케줄 가능한 워커 두 대와 Ready API Pod 두 개가 필요합니다.")
            pods.sort(key=lambda p: (p["spec"]["nodeName"], p["metadata"]["name"]))
            victim = pods[case.get("pod_index", 0) % len(pods)]
            node, pod = victim["spec"]["nodeName"], victim["metadata"]["name"]
            details.update(node=node, original=[p["metadata"]["uid"] for p in pods])
            kubectl("cordon", node)
            stack.callback(kubectl, "uncordon", node)
            eviction = {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": pod, "namespace": "demo"},
                        "deleteOptions": {"preconditions": {"uid": victim["metadata"]["uid"]}}}
            kubectl("create", "--raw", f"/api/v1/namespaces/demo/pods/{pod}/eviction", "-f", "-", data=json.dumps(eviction))
        else:
            if name == "pending":
                cpu = str(case.get("requested_cpu", 999))
                changes = {CONTAINER + "/resources/requests/cpu": cpu, CONTAINER + "/resources/limits/cpu": cpu}
            elif name == "image-pull":
                changes = {CONTAINER + "/image": "ghcr.io/stefanprodan/podinfo:kubeops-missing-" + uid}
            elif name == "metrics-gap":
                changes = {TEMPLATE + "/metadata/annotations/prometheus.io~1scrape": "false"}
            elif name == "pvc":
                claim = create_owned(stack, {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "lab-claim-" + uid}, "spec": {"storageClassName": "missing-" + uid,
                    "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": f"{case.get('volume_mib', 1)}Mi"}}}})
                details["claim"] = claim
                spec = resource("deploy")["spec"]["template"]["spec"]
                changes = {TEMPLATE + "/spec/volumes": [*spec.get("volumes", []),
                    {"name": claim, "persistentVolumeClaim": {"claimName": claim}}],
                    CONTAINER + "/volumeMounts": [*spec["containers"][0].get("volumeMounts", []),
                    {"name": claim, "mountPath": "/mnt/" + claim}]}
            else:
                raise ValueError(name)
            installed = save_fields(stack, "deploy/api", "demo", changes)
            patch("deploy/api", [{"op": "add", "path": key, "value": val} for key, val in changes.items()])
            installed.update(changes)
        yield details
