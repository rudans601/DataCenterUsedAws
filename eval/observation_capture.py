"""복구 후에도 동일한 시간 구간의 관측 Pod 로그를 회수할 수 있다."""
from contextlib import contextmanager
import json
import os
import time
import subprocess

from eval.cluster_fixtures import KUBECTL, control_log, kubectl, record_control
from kubeops_copilot.observations import http_samples, instant, now


def observer_logs(start):
    # kubectl logs의 --since-time은 RFC3339이다. 회수 시각으로 측정 시각을 대체하지 않는다.
    return kubectl("logs", "deploy/http-observer", "-c", "observer", "--since-time", start,
                   namespace="traffic").splitlines()


def fresh_probe():
    start = now()
    until = time.monotonic() + 16
    while time.monotonic() < until:
        time.sleep(1)
        samples = http_samples(observer_logs(start), os.environ["KUBEOPS_EKS_CREATED_AT"], start, now())
        if samples:
            return samples[0]
    raise TimeoutError("독립 HTTP 관측 표본을 회수하지 못했습니다. 앱 상태는 미확정입니다.")


def current_client(after=None):
    # 모델에 제공할 직접 요청은 채점용 4건과 별도로 전송한다.
    until = time.monotonic() + 16
    while True:
        lines = kubectl("logs", "deploy/http-observer", "-c", "observer", "--tail=1", namespace="traffic")
        if lines.strip():
            item = json.loads(lines)
            at = instant(item["client"]["at"])
            age = (instant(now()) - at).total_seconds()
            if (0 <= age <= 15 and (after is None or at > instant(after))
                    and item["cluster"] == os.environ["KUBEOPS_EKS_CREATED_AT"]
                    and item["source"] == "traffic/http-observer" and item["target"] == "demo/api"):
                return item
        if after is None or time.monotonic() >= until:
            break
        time.sleep(1)
    raise RuntimeError("최근 독립 HTTP 관측을 조회하지 못했습니다.")


def check_read():
    """에이전트 조회가 예외로 끝나도 별도의 읽기 요청 결과를 보존한다."""
    try:
        proc = subprocess.run([*KUBECTL, "--request-timeout=3s", "-n", "demo", "get", "deployment/api", "-o", "json"],
                              capture_output=True, text=True, timeout=6)
        if proc.returncode:
            error = proc.stderr.lower()
            result = "timeout" if any(k in error for k in ("timeout", "timed out", "deadline exceeded")) else "error"
            record_control("read", "demo/api · 운영자 관측", result)
            return
        obj = json.loads(proc.stdout)
        record_control("read", "demo/api · 운영자 관측", "ok")
        record_control("controller", "demo/api", "observed", desired=obj["spec"].get("replicas", 1),
                       generation=obj["metadata"]["generation"], observed_generation=obj.get("status", {}).get("observedGeneration"))
    except subprocess.TimeoutExpired:
        record_control("read", "demo/api · 운영자 관측", "timeout")
    except (OSError, KeyError, ValueError):
        record_control("read", "demo/api · 운영자 관측", "error")


@contextmanager
def capture(row, phase):
    incident = {"incident_id": row["incident_id"], "cluster": os.environ["KUBEOPS_EKS_CREATED_AT"],
                "target": "demo/api", "phase": phase, "start": now(), "control": []}
    row.setdefault("observation_windows", []).append(incident)
    token = control_log.set(incident["control"])
    try:
        check_read()
        yield
    finally:
        check_read()
        incident["end"] = now()
        control_log.reset(token)
        parent = control_log.get()
        if parent is not None:
            parent.extend(incident["control"])
