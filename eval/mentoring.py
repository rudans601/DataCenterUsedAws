"""맥북 kind 데모에서 동일한 질의로 관찰·승인·인계를 평가한다."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kubeops_copilot import first_response, k8s_tools, metrics
from eval.observation_capture import current_client
from eval.cluster_fixtures import EKS_LAB, KUBECTL, api_pods, kubectl, resource

# API 부하를 조정하면 loadgen-api가 교체되므로 고정된 웹 부하 생성기에서 측정한다.
PROBE_CLIENT = "deploy/loadgen-web"


def probe(host):
    if EKS_LAB and host == "api.demo.svc.cluster.local":
        sample = current_client()["client"]
        return sample["code"], sample["ms"]
    raw = kubectl("exec", PROBE_CLIENT, "--", "fortio", "load", "-n", "1", "-c", "1", "-qps", "0",
                  "-allow-initial-errors", "-quiet", "-timeout", "2s", "-json", "-", f"http://{host}:9898/", namespace="traffic")
    result = json.loads(raw)
    return int(next(iter(result["RetCodes"]))), round(1000 * result["DurationHistogram"]["Avg"], 1)


def observe(context=None):
    sample = k8s_tools.deployment_snapshot("demo", "api", window="2m")
    svc = next(s for s in k8s_tools.service_status("demo") if s["name"] == "api")
    if EKS_LAB:
        report = current_client()
        client = report["client"]
        api_code, api_ms, api_observed_at = client["code"], client["ms"], client["at"]
        last = report["echo"]["attempts"][-1]
        code, echoed, echo_ms = last["status"], last["verified"], last["ms"]
    else:
        api_code, api_ms = probe("api.demo.svc.cluster.local")
        api_observed_at = datetime.now().astimezone().isoformat()
        start = time.monotonic()
        try:
            response = requests.post(first_response.ECHO_URL, data="kubeops-check", timeout=4)
            code = response.status_code
            echoed = code == 200 and response.json() == ["kubeops-check"]
        except (requests.RequestException, ValueError):
            code, echoed = None, False
        echo_ms = round(1000 * (time.monotonic() - start))
    m = sample["metrics"]
    observed = {"api_observed_at": api_observed_at, "api_code": api_code, "api_ms": api_ms, "ready": sample["ready"], "desired": sample["desired"],
                "endpoints": svc["ready_endpoints"], "http_status": code, "echo_ok": echoed,
                "echo_ms": echo_ms, "p95_ms": m["p95_ms"],
                "rps": m["rps"], "error_ratio": m["error_ratio"], "cpu_util": m.get("cpu_util"),
                "throttle": m.get("throttle_ratio")}
    if context:
        pods = api_pods()
        active = {p["metadata"]["name"] for p in pods}
        statuses = [c for p in pods for c in p["status"].get("containerStatuses", [])]
        observed["container_reasons"] = [status["reason"] for c in statuses for part in ("state", "lastState")
            for status in c.get(part, {}).values() if status.get("reason")]
        warnings = [e for e in k8s_tools.recent_events("demo") if e["object"] in active and e["type"] == "Warning"]
        observed["pod_events"] = [e["reason"] + " " + e["message"] for e in warnings]
        observed["last_pod_warning"] = max((e.get("last_seen_epoch", 0) for e in warnings), default=0)
        for name, data in context.items():
            if name == "transient-warning":
                observed["warning_seen"] = any(e["reason"] == "UpstreamTimeout" and e["object"] == "api"
                    and str(data["timeout_ms"]) + "ms" in e["message"] for e in k8s_tools.recent_events("demo"))
            if name == "dns":
                # Fortio 상태 -1만으로 DNS 장애로 판정하지 않고 실제 resolver 메시지를 확인한다.
                result = subprocess.run([*KUBECTL, "-n", "traffic", "exec", PROBE_CLIENT,
                    "--", "fortio", "curl", "-timeout", "2s", "http://" + data["hostname"] + ":9898/"],
                    capture_output=True, text=True, timeout=30)
                observed["dns_error"] = next((line[-450:] for line in result.stderr.splitlines()
                                              if "no such host" in line.lower()), "")
                ip = resource("svc")["spec"]["clusterIP"]
                observed["ip_probe"] = probe(ip)[0]
            if name == "pvc":
                observed["claim_phase"] = resource("pvc", data["claim"])["status"]["phase"]
            if name == "maintenance":
                observed["cordoned_worker"] = data["node"] if resource("node", data["node"])["spec"].get("unschedulable") else None
                observed["replacement_nodes"] = [p["spec"]["nodeName"] for p in pods
                    if p["metadata"]["uid"] not in data["original"] and p["spec"].get("nodeName") != data["node"]
                    and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))]
            if name == "metrics-gap":
                observed["prometheus_up"] = metrics.is_up()
            if name == "high-load":
                loadgen = resource("deploy", "loadgen-api", "traffic")
                observed["request_seconds_per_second"] = metrics.scalar(
                    'sum(rate(http_request_duration_seconds_sum{namespace="demo",app="api",path!~"healthz|readyz|metrics"}[1m]))')
                observed["loadgen_ready"] = loadgen["status"].get("updatedReplicas") == loadgen["status"].get("readyReplicas") == 2
    return observed


def confirmed(sample, name):
    events = " ".join(sample.get("pod_events", [])).lower()
    reasons = sample.get("container_reasons", [])
    if name == "pending":
        return "insufficient cpu" in events
    if name == "image-pull":
        return any(r in reasons for r in ("ImagePullBackOff", "ErrImagePull"))
    if name == "crashloop":
        return "CrashLoopBackOff" in reasons
    if name == "oom":
        return "OOMKilled" in reasons
    if name == "probe-fail":
        return "readiness probe failed" in events and "404" in events
    if name == "pvc":
        return sample.get("claim_phase") == "Pending" and ("unbound" in events or "persistentvolumeclaim" in events)
    if name == "dns":
        return bool(sample.get("dns_error")) and sample.get("ip_probe") == 200
    if name == "maintenance":
        return bool(sample.get("cordoned_worker") and sample.get("replacement_nodes")) and sample["api_code"] == 200
    if name == "metrics-gap":
        return sample.get("prometheus_up") and sample["api_code"] == 200 and sample["rps"] is None and sample["p95_ms"] is None
    if name == "high-load":
        return sample.get("loadgen_ready") and ((sample["rps"] or 0) > 100 or (sample.get("request_seconds_per_second") or 0) >= 5)
    if name == "transient-warning":
        return sample.get("warning_seen", False)
    if sample["ready"] != sample["desired"] or sample["desired"] == 0:
        return False
    if name == "service-broken":
        return sample["endpoints"] == 0 and sample["api_code"] != 200
    if sample["api_code"] != 200 or not sample["echo_ok"] or not sample["endpoints"] or not sample["rps"] or sample["p95_ms"] is None:
        return False
    if name == "bad-deploy":
        return sample["api_ms"] >= 300 and sample["p95_ms"] > 300
    return sample["p95_ms"] <= 250 and sample["error_ratio"] is not None and sample["error_ratio"] <= 0.01


def wait_sample(name, context=None, timeout=210):
    deadline = time.monotonic() + timeout
    while True:
        sample = observe(context)
        if confirmed(sample, name):
            return sample
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name} 실측 조건 시간초과: {sample}")
        time.sleep(4)


if __name__ == "__main__":
    # 기존 CLI 인자를 유지하면서 현재의 블라인드 평가 프로토콜을 사용한다.
    from runpy import run_path
    run_path(str(ROOT / "eval" / "single_detection.py"), run_name="__main__")
