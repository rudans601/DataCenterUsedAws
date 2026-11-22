"""시드별 단일 상황을 kind에서 재현하고 복구 전에 수집한 HTTP 정답과 판정을 기록한다."""
import argparse
from contextlib import contextmanager, ExitStack, nullcontext
from datetime import datetime
import fcntl
import json
from pathlib import Path
import math
import os
import re
import signal
import subprocess
import tempfile
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kubernetes.utils.quantity import parse_quantity
from eval.cluster_fixtures import ARGS, EKS_LAB, KUBECTL, TEMPLATE, MISSING, api_pods, inject, kubectl, patch, resource, restore_fields, value
from eval.observation_capture import capture, current_client, fresh_probe, observer_logs
from kubeops_copilot.observations import control_score, http_samples, now
from eval.mentoring import PROBE_CLIENT, confirmed, observe, probe, wait_sample
from kubeops_copilot import first_response, graph, incidents, k8s_tools, metrics, rag
from kubeops_copilot.config import settings
from kubeops_copilot.detection import PROTOCOL, ground_truth
from kubeops_copilot.campaign import campaign_plan, DEFAULT_MIX, validate_mix
from eval.remediation_score import REFERENCE_VERSION, reference_decision, recovery_outcome
from kubeops_copilot.scenarios import EKS_NAMES, single_plan
from kubeops_copilot import control_replay
from eval.eks_replay import evaluate_replay

ADDRESS = "http://api.demo.svc.cluster.local:9898/"
QUESTION = ("관측 구간 중 demo/api의 정상 Service 주소로 보낸 요청이 실패하거나 느려진 구간이 있었나요? "
            "서비스 장애 기준은 한 측정 구간의 실패율 5% 초과 또는 p95 300ms 초과입니다. "
            "대표 요청의 장애 판정과 배포 자체의 문제를 각각 판단하세요. 현재 운영 근거와 런북 원문으로 "
            "어떤 조치가 필요한지, 누가 처리할지 결정하세요. 근거가 부족하면 보류하고 이유를 알려주세요.")
REQUEST_METRICS = {"rps", "error_ratio", "p95_ms", "request_seconds_per_second"}


def temporary_patch(stack, target, namespace, changes):
    obj = resource(*target.split("/"), namespace)
    changes = {path: target_value for path, target_value in changes.items() if value(obj, path) != target_value}
    if not changes:
        return
    previous = {path: value(obj, path) for path in changes}
    stack.callback(restore_fields, target, namespace, obj["metadata"]["uid"], previous, changes)
    ops = [{"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]}]
    for path, prior in previous.items():
        if prior is MISSING:
            # JSON Patch에는 부재 검사 연산이 없으므로 기존 부모를 비교한다.
            parent = path.rsplit("/", 1)[0]
            ops.append({"op": "test", "path": parent, "value": value(obj, parent)})
        else:
            ops.append({"op": "test", "path": path, "value": prior})
    ops.extend({"op": "add", "path": path, "value": val} for path, val in changes.items())
    patch(target, ops, namespace)


def rollout(target="deploy/api", namespace="demo"):
    kubectl("rollout", "status", target, "--timeout=150s", namespace=namespace)


def node_capacity():
    nodes = json.loads(kubectl("get", "nodes", "-l", "!node-role.kubernetes.io/control-plane", "-o", "json"))["items"]
    capacity = {}
    for node in nodes:
        name = node["metadata"]["name"]
        description = kubectl("describe", "node", name)
        allocated = description.partition("Allocated resources:")[2]
        remaining = {}
        for key, unit in (("cpu", "cpu_cores"), ("memory", "memory_mib")):
            hit = re.search(r"^\s*" + key + r"\s+(\S+)\s+\(", allocated, re.M)
            if hit:
                free = parse_quantity(node["status"]["allocatable"][key]) - parse_quantity(hit[1])
                remaining[unit] = round(float(free) / (2**20 if key == "memory" else 1), 2)
        capacity[name] = remaining
    return capacity


def prepare(stack, case):
    c = case["conditions"]
    changes = {"/spec/replicas": c["replicas"]}
    if c["background"] == "healthy_rollout":
        changes[TEMPLATE + "/metadata/annotations/kubeops.local~1exercise-run"] = uuid4().hex[:12]
    temporary_patch(stack, "deploy/api", "demo", changes)
    args = list(resource("deploy", "loadgen-api", "traffic")["spec"]["template"]["spec"]["containers"][0]["args"])
    args[args.index("-qps") + 1] = str(c["baseline_qps"])
    temporary_patch(stack, "deploy/loadgen-api", "traffic", {ARGS: args})
    rollout()
    rollout("deploy/loadgen-api", "traffic")
    if case["case"] in {"crashloop", "oom", "probe-fail", "pending", "image-pull", "pvc"}:
        strategy = {"type": "Recreate"} if c["scope"] == "api" else {
            "type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
        temporary_patch(stack, "deploy/api", "demo", {"/spec/strategy": strategy})


def oracle_probe():
    if EKS_LAB:
        return fresh_probe()
    raw = kubectl("exec", PROBE_CLIENT, "--", "fortio", "load", "-n", "4", "-c", "1", "-qps", "0",
                  "-allow-initial-errors", "-quiet", "-timeout", "1s", "-p", "95", "-json", "-", ADDRESS,
                  namespace="traffic")
    result = json.loads(raw)
    hist = result["DurationHistogram"]
    p95 = next((s["Value"] * 1000 for s in hist.get("Percentiles", []) if s["Percentile"] == 95), None)
    return {"at": datetime.now().astimezone().isoformat(), "count": hist["Count"],
            "failed": sum(n for code, n in result["RetCodes"].items() if not 200 <= int(code) < 300),
            "p95_ms": None if p95 is None else round(p95, 2)}


@contextmanager
def traffic_pulse(qps, seconds, record):
    # 별도 Pod에서 실행하고 kubectl exec의 stdout과 stderr를 분리해 측정 JSON을 보존한다.
    name = "detection-load-" + uuid4().hex[:10]
    image = resource("deploy", "loadgen-api", "traffic")["spec"]["template"]["spec"]["containers"][0]["image"]
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": {"restartPolicy": "Never",
           "activeDeadlineSeconds": seconds + 90, "containers": [{"name": "load", "image": image, "imagePullPolicy": "IfNotPresent",
           "args": ["server"],
           "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}}}]}}
    kubectl("create", "-f", "-", data=json.dumps(pod), namespace="traffic")
    try:
        kubectl("wait", "--for=condition=Ready", "pod/" + name, "--timeout=23s", namespace="traffic")
        command = [*KUBECTL, "-n", "traffic", "exec", name, "--", "fortio",
                   "load", "-quiet", "-allow-initial-errors", "-timeout", "1s", "-qps", str(qps),
                   "-c", "8", "-t", f"{seconds}s", "-json", "-", ADDRESS]
        with tempfile.TemporaryFile(mode="w+t") as out, tempfile.TemporaryFile(mode="w+t") as err:
            with subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err) as load:
                end = time.monotonic() + seconds
                try:
                    yield end
                    code = load.wait(timeout=max(0, end - time.monotonic()) + 45)
                    if code:
                        err.seek(0)
                        raise RuntimeError(f"부하 발생기 종료 코드 {code} · {err.read()[-400:]}")
                finally:
                    if load.poll() is None:
                        load.kill()
            out.seek(0)
            result = json.load(out)
        count = result["DurationHistogram"]["Count"]
        if not count:
            raise RuntimeError("부하 발생기의 실제 전송이 0건입니다.")
        record.append({"target_qps": qps, "seconds": seconds, "sent": count,
                       "actual_qps": result.get("ActualQPS"), "response_codes": result.get("RetCodes", {})})
    finally:
        kubectl("delete", "pod/" + name, "--ignore-not-found", "--wait=false", namespace="traffic")


def visible_snapshot(snapshot, visibility):
    if visibility != "no_request_metrics":
        return snapshot
    return {**snapshot, "metrics": {"api": {k: v for k, v in snapshot.get("metrics", {}).get("api", {}).items()
                                            if k not in REQUEST_METRICS}},
            "recovery_facts": {k: v for k, v in snapshot.get("recovery_facts", {}).items() if k != "previous_p95_ms"}}


def model_evidence(visibility, window, snapshot_out=None):
    book = k8s_tools.EvidenceBook()
    snapshot = k8s_tools.collect_evidence("demo", ["api"], book)
    if snapshot_out is not None:
        snapshot_out.clear()
        snapshot_out.update(visible_snapshot(snapshot, visibility))
    hidden = {"no_app_logs": "logs", "no_request_metrics": "metrics"}.get(visibility)
    rows = [e.to_dict() for e in book.items if e.source != hidden and not (
        visibility == "no_request_metrics" and e.source in ("capacity", "history"))]
    if visibility == "no_request_metrics":
        metrics = snapshot["metrics"].get("api", {})
        resource_metrics = {k: v for k, v in metrics.items() if k.startswith(("cpu_", "mem_", "throttle_"))}
        rows.append({"source": "resources", "target": "demo/api", "severity": "info", "title": "자원 사용량",
                     "detail": json.dumps(resource_metrics)})
    keys = {"api_code", "api_ms", "rps", "p95_ms", "error_ratio", "pod_events", "dns_error",
            "ip_probe", "client_dns_errors", "claim_phase", "cordoned_worker", "replacement_nodes", "prometheus_up"}
    if visibility == "no_request_metrics":
        keys -= REQUEST_METRICS
    at = datetime.now().astimezone().isoformat()
    for item in rows:
        if item["source"] == "metrics":
            item["title"] = f"이동 구간 지표 · 실험 이전 포함 가능 · {at} · " + item["title"]
    observed = {k: v for k, v in window.items() if k in keys - REQUEST_METRICS}
    rolling = {k: v for k, v in window.items() if k in keys & REQUEST_METRICS}
    if rolling:
        rows.append({"source": "metrics", "target": "demo/api", "severity": "info",
                     "title": f"수집 시각 {at} 직전 2분 · 실험 이전 포함 가능", "detail": json.dumps(rolling)})
    if observed:
        rows.append({"source": "probe", "target": "demo/api", "severity": "info", "title": f"실습 클라이언트 관측 · {at}",
                     "detail": json.dumps(observed, ensure_ascii=False)})
    return rows


def observed_context(case, details):
    context = {case["case"]: {**case, **details}}
    window = observe(context)
    if case["case"] == "dns" and case["conditions"]["scope"] == "two_clients":
        # 두 번째 독립 Fortio 클라이언트에서도 DNS 실패를 확인한다.
        result = subprocess.run([*KUBECTL, "-n", "traffic", "exec", "deploy/loadgen-api", "--", "fortio",
                                 "curl", "-timeout", "2s", "http://" + details["hostname"] + ":9898/"],
                                capture_output=True, text=True, timeout=20)
        window["client_dns_errors"] = [bool(window.get("dns_error")), "no such host" in (result.stderr + result.stdout).lower()]
    return window


def establish(case, details):
    name = case["case"]
    if name == "bad-deploy":
        rollout()
    deadline = time.monotonic() + 210
    while True:
        window = observed_context(case, details)
        if name in {"normal", "bad-deploy"} or (confirmed(window, name) and all(window.get("client_dns_errors", []))):
            return window
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{name} 재현 신호 시간 초과")
        time.sleep(2)


def service_reading(after):
    if EKS_LAB:
        return current_client(after=after)["client"]
    code, latency = probe("api.demo.svc.cluster.local")
    return {"at": now(), "code": code, "ms": latency}


def measure(case, row, evidence, end, sample):
    # 원본 HTTP 정답은 모델에 전달할 근거와 별도 요청으로 수집한다.
    row["probes"].append(oracle_probe())
    evidence.extend(model_evidence(case["conditions"]["visibility"], sample, row.setdefault("_snapshot", {})))
    readings = [{"at": sample["api_observed_at"], "code": sample["api_code"], "ms": sample["api_ms"]}]
    while time.monotonic() < end:
        time.sleep(min(5, max(0, end - time.monotonic())))
        if time.monotonic() < end:
            row["probes"].append(oracle_probe())
            readings.append(service_reading(readings[-1]["at"]))
    # 긴 실험도 최초 기동 순간에 고정되지 않게 마지막 관측 시점의 상태를 함께 전달한다.
    evidence.extend(model_evidence(case["conditions"]["visibility"], {}, row["_snapshot"]))
    row.setdefault("direct_readings", []).extend(readings)
    evidence.append({"source": "probe", "target": "demo/api", "severity": "info", "title": f"이번 실험 판정 구간 · {readings[0]['at']} ~ {readings[-1]['at']} · 정상 Service 주소 {ADDRESS} 직접 요청 시계열",
                     "detail": json.dumps(readings)})


def interval_evidence(times):
    # 완전히 포함되는 초만 조회하여 주입 전 요청이 이번 실험 수치에 섞이지 않게 한다.
    injection = datetime.fromisoformat(times["injection_started"]).timestamp()
    end = int(datetime.fromisoformat(times["measured_until"]).timestamp())
    seconds = end - math.ceil(injection)
    if seconds < 1:
        return []
    result = []
    for boundary, source, title in ((int(injection), "history", "주입 직전 동일 길이 비교 구간"),
                                    (end, "metrics", "이번 실험 주입 구간")):
        values = metrics.request_metrics("demo", "api", f"{seconds}s", at=boundary)
        begin = datetime.fromtimestamp(boundary-seconds).astimezone().isoformat()
        finish = datetime.fromtimestamp(boundary).astimezone().isoformat()
        result.append({"source": source, "target": "demo/api", "severity": "info",
                       "title": f"{title} · {begin} ~ {finish}",
                       "detail": json.dumps(values) + " (null은 표본 부족 또는 수집 누락)"})
    return result


def diagnose(evidence, snapshot, incident, app_receipt, visibility):
    def read_again():
        fresh_snapshot = {}
        readings = []
        after = now()
        for _ in range(3):
            reading = service_reading(after)
            readings.append(reading)
            after = reading["at"]
        fresh = model_evidence(visibility, {}, fresh_snapshot)
        fresh.append({"source": "probe", "target": "demo/api", "severity": "info",
                      "title": "최초 판정 구간 이후의 대표 Service 직접 요청", "detail": json.dumps(readings)})
        return {"evidence": fresh, "snapshot": fresh_snapshot}

    inputs = [{**item, "id": f"E{i}"} for i, item in enumerate(evidence, 1)]
    state = {"namespace": "demo", "question": QUESTION, "evidence": inputs, "snapshot": snapshot,
             "signals": [], "intent": {"targets": ["api"]}, "observations": {"echo 요청 결과": app_receipt},
             "approval_source": "experiment", "thread_id": incident}
    state.update(graph.retrieve(state))
    state.update(graph.analyze(state))
    state.update(graph.remeasure_once(state, read_again))
    state.update(graph.guard(state))
    return state


@contextmanager
def verification_load(qps):
    """유한 부하 시험과 별도로 같은 요청률을 유지해 부하 종료를 복구로 오인하지 않게 한다."""
    image = resource("deploy", "loadgen-api", "traffic")["spec"]["template"]["spec"]["containers"][0]["image"]
    name = "verify-load-" + uuid4().hex[:10]
    obj = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": {
        "restartPolicy": "Never", "activeDeadlineSeconds": 620, "containers": [{"name": "client", "image": image,
        "args": ["load", "-quiet", "-allow-initial-errors", "-c", "8", "-qps", str(qps), "-t", "600s", ADDRESS],
        "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}}}]}}
    kubectl("create", "-f", "-", data=json.dumps(obj), namespace="traffic")
    try:
        kubectl("wait", "--for=condition=Ready", "pod/" + name, "--timeout=30s", namespace="traffic")
        yield name
    finally:
        kubectl("delete", "pod/" + name, "--ignore-not-found", "--wait=false", namespace="traffic")


def operator_decision(state, approver, approval_dir, timeout=120):
    p = state["guard"]["normalized"]
    if approver and (p["namespace"], p["deployment"], p["action"]) == ("demo", "api", "rollback"):
        return {"approved": True, "approver": approver, "comment": "명시한 시험 롤백"}
    if not approval_dir:
        return {"approved": False, "approver": "평가", "comment": "승인 미제공"}
    request_id = uuid4().hex
    pending = approval_dir / "pending.json"
    tmp = pending.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": request_id, "ticket_id": state.get("triage", {}).get("ticket_id"), "proposal": p, "reason": state["proposal"]["reason"],
                               "impact": state["proposal"]["expected_impact"], "risk": state["proposal"]["risk"],
                               "citation": state["analysis"]["runbook_citation"],
                               "version": state["guard"]["target_version"]}, ensure_ascii=False))
    tmp.replace(pending)
    answer = approval_dir / (request_id + ".json")
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if answer.exists():
                decision = json.loads(answer.read_text())
                return {"approved": decision.get("approved") is True,
                        "approver": str(decision.get("approver") or "운영자"), "comment": "실습 화면 결정"}
            time.sleep(.5)
        return {"approved": False, "approver": "평가", "comment": "승인 시간 초과"}
    finally:
        pending.unlink(missing_ok=True)


def assess_and_act(row, evidence, snapshot, stack, approver, approval_dir, original, load_pod=None):
    # 정답·유형·시드·정리 방식은 모델이 받는 state에 포함시키지 않는다.
    try:
        print("단계 · 에이전트가 운영 근거와 런북을 확인하고 있습니다.", flush=True)
        state = diagnose(evidence, snapshot, row["incident_id"], row["app_checks"][-1], row["conditions"]["visibility"])
    except Exception as exc:
        timed_out = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        row.update(prediction_status="timeout" if timed_out else "error", prediction_error=type(exc).__name__)
        incidents.issue(row["incident_id"], "진단 실패", "에이전트가 진단을 완료하지 못했습니다.",
                        {"오류": type(exc).__name__, "대상": "demo/api"})
        return
    row.update(recheck=state["recheck"], evidence=state["evidence"])
    a, route = state["analysis"], state["triage"]["route"]
    row.update(prediction=a["service_detection"], prediction_status="complete", diagnosis=a["summary"],
               category=a["root_cause_category"], root_cause=a["root_cause"], manual_steps=a["manual_steps"], proposed_action=a["proposal"]["action"], proposal=a["proposal"],
               model_impact=a["impact"], model_route=a["route"], effective_route=route, decision=state["triage"],
               findings=a["findings"], citation=a["runbook_citation"], citation_source=a["citation_source"],
               cited_procedures=a["cited_procedures"],
               documents=[{k: d.get(k) for k in ("id", "source", "title", "version", "matched_events")} for d in state["docs"]])
    if row.get("judgement_only"):
        row["agent_case_id"] = graph.report(state)["case_id"]
        return
    p = state["guard"]["normalized"]
    if route in ("approval", "auto") and (p["namespace"], p["deployment"]) != ("demo", "api"):
        row["execution_status"] = "시험 대상 범위 밖"
        return
    if route == "approval":
        state["approval"] = operator_decision(state, approver, approval_dir)
        row["approval"] = state["approval"]
    if route == "auto" or (route == "approval" and state["approval"]["approved"]):
        before = resource("deploy")
        row["pre_action_probes"] = [oracle_probe(), oracle_probe()]
        # 실험 소유 필드만 복원하며, 다른 작업자의 덮어쓰기는 충돌로 남긴다.
        state.update(graph.execute(state))
        row["before_action"] = state["before"]
        row["action_result"] = state["execution"]
        row["recovery_outcome"] = "검증 불가" if state["execution"].get("ok") else "실행 실패"
        if row["action_result"].get("ok"):
            row["action_actor"] = "agent"
            current = resource("deploy")
            if current["metadata"]["uid"] != before["metadata"]["uid"] or current["metadata"]["generation"] != state["execution"]["generation"]:
                raise RuntimeError("에이전트 변경 직후 다른 변경을 발견해 정리를 중단했습니다.")
            fields = ["/spec/replicas"] if p["action"] == "scale" else [TEMPLATE, "/metadata/annotations/kubernetes.io~1change-cause"]
            stack.callback(restore_fields, "deploy/api", "demo", before["metadata"]["uid"],
                           {path: value(original, path) for path in fields},
                           {path: value(current, path) for path in fields})
        state.update(graph.verify(state))
        row["verification"] = state["verification"]
        if state["execution"].get("ok"):
            row["after_action"] = observe()
            row["after_action_probes"] = [oracle_probe(), oracle_probe()]
            if load_pod:
                row["verification_load_active"] = resource("pod", load_pod, "traffic")["status"]["phase"] == "Running"
        row["recovery_outcome"] = recovery_outcome(row)
        if row["recovery_outcome"] in ("서비스 복구", "배포 복구"):
            row["recovered_by"] = "agent"
        else:
            row["next_owner"] = "handoff"
            if state["verification"]["verdict"] == "success":
                state["verification"]["verdict"] = "failed" if row["recovery_outcome"] == "복구 실패" else "unknown"
                state["verification"]["summary"] = "실험 정리 전 독립 검증 결과 · " + row["recovery_outcome"]
    row["agent_case_id"] = graph.report(state)["case_id"]


def app_check(row, evidence, url=first_response.ECHO_URL):
    if EKS_LAB and url == first_response.ECHO_URL:
        receipt = current_client()["echo"]
    else:
        receipt = first_response.echo("kubeops-check", url)
    row["app_checks"].append(receipt)
    incidents.record_echo(row["incident_id"], len(row["app_checks"]), receipt)
    if receipt["outcome"] != "healthy":
        evidence.append({"source": "probe", "target": "demo/api", "severity": "warning",
                         "title": "앱의 실제 echo 요청과 재시도 결과", "detail": json.dumps(receipt, ensure_ascii=False)})


def app_resolved(checks, samples, readings):
    if not checks or not samples or not any(r["outcome"] == "recovered" for r in checks):
        return False
    if not readings or any(r.get("code") != 200 or r.get("ms") is None or r["ms"] > 300 for r in readings):
        return False
    return all(r["outcome"] != "escalated" for r in checks) and all(
        s.get("api_code") == 200 and s.get("echo_ok") and s.get("endpoints", 0) > 0
        and s.get("ready") == s.get("desired") and not s.get("container_reasons")
        and not s.get("dns_error") and s.get("claim_phase") != "Pending" and not s.get("cordoned_worker")
        and (s.get("p95_ms") is not None and s["p95_ms"] <= 300)
        and (s.get("error_ratio") is not None and s["error_ratio"] <= .05)
        and s.get("last_pod_warning", 0) < s["observation_started"]
        for s in samples)


def evaluate(case, row, fixtures_only, approver, manual, approval_dir=None):
    c = case["conditions"]
    evidence = []
    row.update(tier_criteria="current-http-v1", probes=[], pulses=[], episodes=[], app_checks=[], measurement_status="started")
    samples = []
    selector = resource("svc")["spec"]["selector"]
    with ExitStack() as setup:
        rollout()
        rollout("deploy/loadgen-api", "traffic")
        wait_sample("baseline")
        prepare(setup, case)
        row["baseline"] = wait_sample("baseline")
        row["baseline_at"] = datetime.now().astimezone().isoformat()
        baseline = {k: row["baseline"].get(k) for k in ("api_code", "api_ms", "endpoints", "ready", "desired")}
        if c["visibility"] != "no_request_metrics":
            baseline.update({k: row["baseline"].get(k) for k in REQUEST_METRICS})
        evidence.append({"source": "history", "target": "demo/api", "severity": "info",
                         "title": "이번 실험 주입 전 비교 기준 · " + row["baseline_at"], "detail": json.dumps(baseline)})
        row["remaining_capacity"] = node_capacity()
        time.sleep(c["onset_s"])
        for cycle in range(c["repeat"]):
            if cycle:
                time.sleep(c["gap_s"])
            times = {"round": cycle + 1, "injection_started": datetime.now().astimezone().isoformat()}
            row["episodes"].append(times)
            with ExitStack() as episode, (capture(row, f"주입 {cycle+1}") if EKS_LAB else nullcontext()):
                if EKS_LAB:
                    before_pods = api_pods()
                    row["observation_windows"][-1]["baseline_pod_uids"] = [p["metadata"]["uid"] for p in before_pods]
                original = None if fixtures_only else resource("deploy")
                if case["case"] == "high-load":
                    peak, seconds = case["qps"], c["duration_s"]
                    ramp = c["pattern"] == "step"
                    rate = peak // 2 if ramp else peak
                    with traffic_pulse(rate, seconds, row["pulses"]) as end:
                        times["confirmed_at"] = datetime.now().astimezone().isoformat()
                        sample = observe()
                        if not fixtures_only:
                            app_check(row, evidence)
                        measure(case, row, evidence, end - seconds // 2 if ramp else end, sample)
                        if ramp:
                            with traffic_pulse(peak-rate, max(1, round(end-time.monotonic())), row["pulses"]) as end2:
                                sample = observe()
                                measure(case, row, evidence, min(end, end2), sample)
                else:
                    details = episode.enter_context(inject(case))
                    sample = establish(case, details)
                    times["confirmed_at"] = datetime.now().astimezone().isoformat()
                    if not fixtures_only:
                        app_check(row, evidence, details.get("echo_url", first_response.ECHO_URL))
                    measure(case, row, evidence, time.monotonic() + c["duration_s"], sample)
                if EKS_LAB:
                    api_pods()
                    stages = control_score(row["observation_windows"][-1])
                    evidence.append({"source": "probe", "target": "demo/api", "severity": "info",
                                     "title": "Kubernetes 조회·변경 응답과 새 Pod 배치 관측",
                                     "detail": json.dumps(stages, ensure_ascii=False)})
                times["measured_until"] = datetime.now().astimezone().isoformat()
                if not fixtures_only and c["visibility"] != "no_request_metrics":
                    evidence.extend(interval_evidence(times))
                row["injected"] = sample
                samples.append({**sample, "observation_started": datetime.fromisoformat(times["injection_started"]).timestamp()})
                snapshot = row.pop("_snapshot", {})
                if cycle + 1 != c["repeat"]:
                    continue
                row.update(truth=ground_truth(row["probes"]), measurement_status="complete",
                           evidence=[{**e, "id": f"E{i}"} for i,e in enumerate(evidence, 1)])
                row["reference"] = reference_decision(case, row, snapshot, environment="eks" if EKS_LAB else "kind")
                row["recovered_by"] = ("observe" if case["case"] == "normal" else
                                       "load_ends" if case["case"] == "high-load" else "fixture")
                if fixtures_only:
                    continue
                if not row.get("judgement_only") and app_resolved(row["app_checks"], samples, row["direct_readings"]):
                    row.update(prediction_status="app_recovered", recovered_by="app", recovery_outcome="앱 요청 복구")
                    continue
                incidents.record(row["incident_id"], "agent-intake", "시스템", "에이전트 진단 요청",
                                 "앱 요청 결과와 클러스터의 관측값을 함께 전달했습니다.")
                load_pod = None
                if not row.get("judgement_only") and c["recovery"] == "hold_load":
                    load_pod = episode.enter_context(verification_load(case["qps"]))
                    row["verification_load_qps"] = case["qps"]
                assess_and_act(row, evidence, snapshot, episode, approver, approval_dir, original, load_pod)
                if manual and not row.get("judgement_only") and row.get("effective_route") == "handoff" and resource("svc")["spec"]["selector"] != selector:
                    print("복원 대기 · demo/api Service selector를 복원 · " + json.dumps(selector), flush=True)
                    end = time.monotonic() + 120
                    while time.monotonic() < end:
                        if resource("svc")["spec"]["selector"] == selector:
                            checks = [oracle_probe(), oracle_probe()]
                            if ground_truth(checks) == "normal":
                                row.update(recovered_by="operator", manual_probes=checks, recovery_outcome="운영자 복구 확인")
                                incidents.record(row["incident_id"], "manual-verify", "검증기", "외부 수동 복원 확인",
                                                 "Service selector 복원과 독립 HTTP 측정 2회 정상. 작업자 신원은 별도 기록.")
                                break
                        time.sleep(2)


def choose_cases(seed, choice, mix=None):
    if choice == "campaign":
        return campaign_plan(seed, mix, include_replay=EKS_LAB)
    if not EKS_LAB and choice in (*control_replay.NAMES, "replays"):
        raise ValueError("관리 구성요소 재생은 EKS 실측 환경에서 실행하세요.")
    cases = single_plan(seed, include_replay=EKS_LAB)
    if choice == "replays":
        return [case for case in cases if case.get("mode") == control_replay.MODE]
    if choice == "random":
        cases = cases[:1]
    elif choice != "batch":
        cases = [c for c in cases if c["case"] == choice]
    return cases


def validate_target(server, agent_server):
    if EKS_LAB:
        if server != os.environ.get("KUBEOPS_EKS_VERIFIED_ENDPOINT") or not server.startswith("https://"):
            raise RuntimeError("scripts/eks-lab.sh를 통해 검증된 EKS 실습 환경을 선택하세요.")
    elif urlsplit(server).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("kind API 서버가 로컬 주소여야 합니다.")
    if agent_server != server:
        raise RuntimeError("실험 실행기와 에이전트의 클러스터 주소가 다릅니다.")


def save_report(path, report):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", delete=False) as temp:
        json.dump(report, temp, ensure_ascii=False, indent=2)
    Path(temp.name).replace(path)


def resume_campaign(path, expected):
    report = json.loads(path.read_text())
    if not report.get("campaign_mix") or any(report.get(k) != v for k, v in expected.items()):
        raise ValueError("같은 클러스터·모델·채점 버전으로 실행한 100회 보고서를 선택하세요.")
    rows = report["cases"]
    if len(rows) > 100 or any(r.get("trial") != i for i, r in enumerate(rows, 1)):
        raise ValueError("회차가 연속된 100회 평가 기록이 필요합니다.")
    prior_attempts = report.get("previous_attempts") or []
    if (rows and rows[-1].get("measurement_status") in ("failed", "timeout", "interrupted", "started")
            and not rows[-1].get("prediction")):
        report.setdefault("previous_attempts", []).append(rows.pop())
    if len(rows) == 100:
        raise ValueError("이미 100회가 모두 기록되었습니다.")
    mix = report["campaign_mix"]
    validate_mix(mix, EKS_LAB)
    cases = report.get("campaign_plan")
    if cases is None:
        cases = campaign_plan(report["seed"], mix, EKS_LAB, completed=rows)
    else:
        # 처음 이어갈 때 고정한 구버전 회차 수를 이용해 같은 계획을 다시 검증한다.
        fixed = report.get("campaign_fixed_prefix", prior_attempts[0]["trial"] - 1 if prior_attempts else 0)
        if type(fixed) is not int or not 0 <= fixed <= len(rows):
            raise ValueError("저장된 계획의 고정 회차 수가 올바르지 않습니다.")
        if cases != campaign_plan(report["seed"], mix, EKS_LAB, completed=rows[:fixed]):
            raise ValueError("저장된 계획과 시드로 재현한 계획이 다릅니다.")
    if any(any(row.get(k) != v for k, v in case.items()) for row, case in zip(rows, cases)):
        raise ValueError("완료된 회차와 저장된 계획이 일치하지 않습니다.")
    if "campaign_plan" not in report:
        report["campaign_fixed_prefix"] = len(rows)
    return report, cases


def run(seed, choice, fixtures_only, approver, manual, approval_dir=None, mix=None, stop_file=None, resume=None):
    server = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    validate_target(server, k8s_tools.api_client().configuration.host)
    directory = ROOT / "eval" / "results"
    directory.mkdir(exist_ok=True)
    with (ROOT / ".kube" / "mentoring.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = datetime.now().astimezone()
        metadata = {"environment": "eks" if EKS_LAB else "kind", "protocol": PROTOCOL, "model": settings.llm_model,
                    "runbook_version": rag.RUNBOOK_VERSION, "corpus_revision": rag.corpus_revision(), "prompt_version": graph.PROMPT_VERSION}
        if EKS_LAB:
            metadata["cluster_created_at"] = os.environ["KUBEOPS_EKS_CREATED_AT"]
        if resume is not None:
            path = resume.resolve()
            if path.parent != directory.resolve() or not path.match("detection-*.json"):
                raise ValueError("eval/results 안의 단일 평가 보고서를 지정하세요.")
            report, cases = resume_campaign(path, {**metadata, "workload_reference_version": REFERENCE_VERSION})
            choice = "campaign"
            fixtures_only = report["fixtures_only"]
        else:
            cases = choose_cases(seed, choice, mix)
            path = directory / f"detection-{started:%Y%m%d-%H%M%S-%f}.json"
            report = {**metadata, "started": started.isoformat(), "seed": seed, "fixtures_only": fixtures_only, "cases": []}
            if any(case.get("mode") != control_replay.MODE for case in cases):
                report["workload_reference_version"] = REFERENCE_VERSION
            if choice == "campaign":
                report["campaign_mix"] = DEFAULT_MIX if mix is None else mix
                report["campaign_fixed_prefix"] = 0
        offset = len(report["cases"])
        if choice == "campaign":
            report["campaign_plan"] = cases
            save_report(path, report)
        for index, case in enumerate(cases[offset:], offset + 1):
            if stop_file and stop_file.exists():
                break
            print(f"단계 · {index}/{len(cases)}회", flush=True)
            row = {**case, "incident_id": uuid4().hex}
            if choice == "campaign" and not fixtures_only:
                row["judgement_only"] = True
            report["cases"].append(row)
            print("실행", case["case"], flush=True)
            print("사건", row["incident_id"], flush=True)
            simulated = case.get("mode") == control_replay.MODE
            try:
                if simulated:
                    evaluate_replay(case, row, fixtures_only)
                else:
                    with capture(row, "전체 실험") if EKS_LAB else nullcontext():
                        evaluate(case, row, fixtures_only, approver, manual, approval_dir)
            except KeyboardInterrupt:
                row["measurement_status"] = "interrupted"
                raise
            except Exception as exc:
                timed_out = ("시간초과" in str(exc).replace(" ", "") or "timeout" in type(exc).__name__.lower()
                             or "timed out" in str(exc).lower())
                incomplete = "timeout" if timed_out else "failed"
                if row.get("measurement_status") != "complete":
                    row["measurement_status"] = incomplete
                row["error"] = str(exc)[:1200]
                if not fixtures_only and row.get("measurement_status") == "complete":
                    incidents.issue(row["incident_id"], "실행 검토", "실험 중 조치 처리가 중단됐습니다.", {"오류": type(exc).__name__})
            finally:
                if not simulated:
                    try:
                        rollout()
                        rollout("deploy/loadgen-api", "traffic")
                        row["after_fixture_cleanup"] = wait_sample("baseline")
                        row["recovery_verified_at"] = datetime.now().astimezone().isoformat()
                    except Exception as exc:
                        row["cleanup_error"] = str(exc)[:1200]
                    if not fixtures_only:
                        incidents.record(row["incident_id"], "cleanup", "실험 도구", "실습 환경 원상복구 점검",
                                         row.get("cleanup_error") or "기준 상태 확인 · 장애 조치 실적에서 제외")
                        row["timeline"] = incidents.timeline(row["incident_id"])
                        row["tickets"] = incidents.tickets(row["incident_id"])
                    if EKS_LAB:
                        windows = row["observation_windows"]
                        try:
                            lines = observer_logs(windows[0]["start"])
                            for window in windows:
                                window["http_samples"] = http_samples(lines, window["cluster"], window["start"], window["end"])
                                window["http_score"] = ground_truth(window["http_samples"])
                        except (RuntimeError, subprocess.SubprocessError, ValueError, OSError) as exc:
                            row["observer_read_error"] = type(exc).__name__
                save_report(path, report)
            if row.get("cleanup_error") or (row.get("error") and row.get("measurement_status") == "complete"):
                break
        print("REPORT", path.name, flush=True)
        return int(len(report["cases"]) != len(cases) or any(
            r.get("error") or r.get("cleanup_error") or r.get("prediction_error") or
            r.get("measurement_status") == "insufficient" for r in report["cases"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--case", choices=["batch", "random", "campaign", "replays", *EKS_NAMES], default="batch")
    parser.add_argument("--fixtures-only", action="store_true")
    parser.add_argument("--plan", action="store_true", help="실행 없이 시드별 단일 조건을 출력")
    parser.add_argument("--approve", metavar="승인자")
    parser.add_argument("--wait-operator", action="store_true")
    parser.add_argument("--approval-dir", type=Path, help="이번 실행의 운영자 승인 응답 폴더")
    parser.add_argument("--mix", type=int, nargs=4, metavar=("정상", "주의", "장애", "중대"), help="100회 목표 비율")
    parser.add_argument("--stop-file", type=Path, help="현재 사건 정리 후 다음 사건을 시작하지 않을 신호 파일")
    parser.add_argument("--resume", type=Path, help="기존 100회 보고서의 다음 회차부터 재개")
    args = parser.parse_args()
    if args.resume and (args.plan or args.mix or args.approve or args.wait_operator or args.approval_dir or args.fixtures_only or args.case != "batch"):
        parser.error("--resume은 기존 보고서에 저장된 계획과 모델 사용 설정을 따릅니다.")
    mix = dict(zip(DEFAULT_MIX, args.mix)) if args.mix is not None else None
    if mix is not None and (args.case != "campaign" or min(args.mix) < 0 or sum(args.mix) != 100):
        parser.error("--mix는 campaign에서 사용하며 합계가 100인 음이 아닌 네 정수를 지정합니다.")
    if args.plan:
        print(json.dumps(choose_cases(args.seed, args.case, mix), ensure_ascii=False, indent=2))
        raise SystemExit(0)

    def stop(signum, frame):
        raise KeyboardInterrupt("실습 중지")
    signal.signal(signal.SIGTERM, stop)
    raise SystemExit(run(args.seed, args.case, args.fixtures_only, args.approve, args.wait_operator, args.approval_dir, mix, args.stop_file, args.resume))
