"""전용 EKS의 임시 Deployment에서 재시작·기본 롤백·정상 버전 복원을 비교한다."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
K = ["kubectl", "--kubeconfig", str(ROOT / ".kube/eks-admin.kubeconfig"), "--context", "datacenterusedaws-lab"]


def call(*args, data=None, ns="demo"):
    return subprocess.check_output([*K, "--request-timeout=15s", "-n", ns, *args],
                                   input=data, text=True, timeout=80).strip()


def read(kind, name):
    return json.loads(call("get", kind, name, "-o", "json"))


def probe(host, path="/"):
    code = """import http.client,json,sys
results=[]
for _ in range(4):
 c=http.client.HTTPConnection(sys.argv[1],9898,timeout=1)
 try:
  c.request('GET',sys.argv[2]);r=c.getresponse();r.read();results.append(r.status)
 except OSError: results.append(0)
 finally: c.close()
print(json.dumps(results))"""
    return json.loads(call("exec", "deploy/http-observer", "-c", "observer", "--", "python", "-c", code, host, path,
                           ns="traffic"))


def state(name, expected_ready, after_revision=0):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        dep = read("deploy", name)
        rev = int(dep["metadata"]["annotations"].get("deployment.kubernetes.io/revision", 0))
        sets = json.loads(call("get", "rs", "-l", f"app={name}", "-o", "json"))["items"]
        current_sets = {s["metadata"]["uid"] for s in sets
                        if int(s["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", 0)) == rev}
        pods = json.loads(call("get", "pods", "-l", f"app={name}", "-o", "json"))["items"]
        active = [p for p in pods if not p["metadata"].get("deletionTimestamp") and any(
            o["uid"] in current_sets for o in p["metadata"].get("ownerReferences", []))]
        if rev > after_revision and len(active) == 1:
            pod = active[0]
            ready = any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []))
            events = json.loads(call("get", "events", "--field-selector", f"involvedObject.uid={pod['metadata']['uid']}", "-o", "json"))["items"]
            messages = [e.get("message", "") for e in events if e.get("reason") == "Unhealthy"]
            if (pod.get("status", {}).get("phase") == "Running" and ready == expected_ready
                    and (ready or any("404" in message for message in messages))):
                endpoints = read("endpoints", name)
                count = sum(len(s.get("addresses", [])) for s in endpoints.get("subsets", []))
                if bool(count) != ready:
                    time.sleep(2)
                    continue
                return {"at": datetime.now().astimezone().isoformat(), "revision": rev,
                        "phase": pod["status"]["phase"], "ready": ready,
                        "restarts": sum(c.get("restartCount", 0) for c in pod["status"].get("containerStatuses", [])),
                        "probe_path": dep["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"],
                        "ready_endpoints": count, "readiness_events": messages,
                        "service_http": probe(f"{name}.demo.svc.cluster.local"),
                        "pod_readyz_http": probe(pod["status"]["podIP"], "/readyz")}
        time.sleep(3)
    raise TimeoutError("임시 Readiness 실험의 기대 Pod 상태를 확인하지 못했습니다.")


def run():
    os.umask(0o077)
    account = subprocess.check_output(["aws", "sts", "get-caller-identity", "--region", "ap-northeast-2",
                                       "--query", "Account", "--output", "text"], text=True).strip()
    if account != os.environ["KUBEOPS_AWS_ACCOUNT"]:
        raise RuntimeError("AWS 계정 불일치")
    cluster = json.loads(subprocess.check_output(["aws", "eks", "describe-cluster", "--name", "datacenterusedaws-lab",
                                                 "--region", "ap-northeast-2", "--output", "json"], text=True))["cluster"]
    if (cluster["tags"].get("Project") != "DataCenterUsedAws" or cluster["tags"].get("Purpose") != "scenario-lab"
            or call("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}") != cluster["endpoint"]):
        raise RuntimeError("전용 실습 클러스터가 아닙니다.")
    original = read("deploy", "api")
    name = "trial22-check-" + uuid4().hex[:8]
    spec = deepcopy(original["spec"])
    spec.update(replicas=1, selector={"matchLabels": {"app": name}}, strategy={"type": "Recreate"})
    spec["template"]["metadata"] = {"labels": {"app": name}}
    c = spec["template"]["spec"]["containers"][0]
    if c["readinessProbe"]["httpGet"]["path"] != "/readyz":
        raise RuntimeError("원본 API가 정상 경로가 아니므로 복제 실험을 중단합니다.")
    objects = [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name}, "spec": spec},
               {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
                "spec": {"selector": {"app": name}, "ports": [{"port": 9898, "targetPort": 9898}]}}]
    output = ROOT / "eval/comparisons" / (name + ".json")
    output.parent.mkdir(exist_ok=True)
    report = {"mode": "isolated-eks-readiness-recovery", "target": name, "observations": []}
    owned = []
    def record(label, ready, previous=0):
        result = state(name, ready, previous)
        report["observations"].append({"step": label, **result})
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(label, {k: result[k] for k in ("revision", "phase", "ready", "probe_path", "ready_endpoints", "service_http")}, flush=True)
        return result["revision"]
    try:
        for obj in objects:
            created = json.loads(call("create", "-f", "-", "-o", "json", data=json.dumps(obj)))
            owned.append((obj["kind"].lower(), created["metadata"]["uid"]))
        healthy = record("baseline", True)
        call("patch", "deploy/" + name, "--type=json", "-p", json.dumps([
            {"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/httpGet/path", "value": "/ready"}]))
        bad = record("readiness_failure", False, healthy)
        call("rollout", "restart", "deploy/" + name)
        restarted = record("restart", False, bad)
        call("rollout", "undo", "deploy/" + name)
        reverted = record("default_rollback", False, restarted)
        call("rollout", "undo", "deploy/" + name, f"--to-revision={healthy}")
        record("known_healthy_rollback", True, reverted)
    finally:
        report["cleanup"] = []
        for kind, uid in reversed(owned):
            if read(kind, name)["metadata"]["uid"] != uid:
                raise RuntimeError("임시 리소스 소유자가 변경돼 삭제를 중단했습니다.")
            call("delete", kind, name, "--wait=true", "--timeout=60s")
            report["cleanup"].append(kind)
        after = read("deploy", "api")
        report["original_api_unchanged"] = original["spec"] == after["spec"] and original["metadata"]["uid"] == after["metadata"]["uid"]
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("REPORT", output.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    run()
