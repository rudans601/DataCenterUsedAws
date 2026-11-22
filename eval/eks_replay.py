"""기존 EKS 워커의 HTTP 구간에 합성 운영 기록을 맞춘다. 재생 경로는 클러스터를 변경하지 않는다."""
from datetime import datetime, timezone
import json
import os
import subprocess

from eval.cluster_fixtures import kubectl
from eval.observation_capture import observer_logs
from kubeops_copilot import graph
from kubeops_copilot.control_replay import make_window
from kubeops_copilot.observations import instant, replay


def endpoint_sample():
    try:
        obj = json.loads(kubectl("get", "endpointslices", "-l", "kubernetes.io/service-name=api", "-o", "json"))
        addresses = {addr for item in obj["items"] for ep in item["endpoints"]
                     if ep.get("conditions", {}).get("ready") is not False for addr in ep["addresses"]}
        return {"at": datetime.now(timezone.utc).isoformat(), "ready": len(addresses), "source": "eks-endpointslices"}
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None


def evaluate_replay(case, row, fixtures_only):
    endpoints = endpoint_sample()
    cluster = os.environ["KUBEOPS_EKS_CREATED_AT"]
    window = make_window(case, row["incident_id"], cluster, datetime.now(timezone.utc))
    if endpoints:
        window["eks_endpoints"] = endpoints
    row["observation_windows"] = [window]
    lines = observer_logs(window["start"])
    result = replay(window, lines)
    ref = result["reference"]
    window.update(http_samples=result["samples"], http_score=result["http"])
    row.update(truth=ref["http"], reference=ref, probes=result["samples"], measurement_status="complete")
    if ref["impact"] == "unknown":
        row["measurement_status"] = "insufficient"
    if fixtures_only:
        return

    evidence = [{"id": "E1", "source": "events", "target": "demo/api", "severity": "info",
                 "title": "같은 사건의 Kubernetes 기록 (합성)", "detail": json.dumps(window["control"], ensure_ascii=False)}]
    # 별도로 전송된 클라이언트 요청만 모델에 제공한다. 채점용 4건 묶음은 공개하지 않는다.
    clients = []
    for line in lines:
        item = json.loads(line)
        sample = item.get("client")
        if ((item.get("cluster"), item.get("source"), item.get("target")) ==
                (cluster, "traffic/http-observer", "demo/api") and sample
                and instant(window["assess_from"]) <= instant(sample["at"]) <= instant(window["end"])):
            clients.append({k: sample[k] for k in ("at", "ms", "code")})
    evidence.append({"id": "E2", "source": "probe", "target": "demo/api", "severity": "info",
                     "title": "EKS 워커 앱 요청 실측", "detail": json.dumps(clients)})
    if endpoints:
        evidence.append({"id": "E3", "source": "probe", "target": "demo/api", "severity": "info",
                         "title": "EKS EndpointSlice 실측", "detail": json.dumps(endpoints)})
    state = {"control_replay": True, "namespace": "demo", "question": "사건 구간의 HTTP 영향과 최고 위험 등급, 마지막 관측에서 필요한 처리 방식을 판단하세요. "
             "조회·쓰기·생성·배치 중 확인한 단계와 원인의 근거, 회복 여부를 설명하세요.",
             "intent": {"targets": ["api"]}, "evidence": evidence, "signals": []}
    row["evidence"] = evidence
    try:
        state.update(graph.retrieve(state))
        analysis = graph.analyze(state)["analysis"]
    except Exception as exc:
        row.update(prediction_status="error", prediction_error=type(exc).__name__)
        return
    route = analysis["route"]
    row.update(prediction=analysis["service_detection"], prediction_status="complete",
               model_route=route, model_impact=analysis["impact"], proposed_action=analysis["proposal"]["action"],
               proposal=analysis["proposal"], diagnosis=analysis["summary"], root_cause=analysis["root_cause"],
               citation=analysis.get("runbook_citation"), citation_source=analysis.get("citation_source"),
               runbook_version=analysis.get("runbook_version"),
               manual_steps=analysis["manual_steps"], decision={"reason": analysis["decision_reason"]},
               impact_correct=(analysis["impact"] == ref["impact"]), route_correct=(route == ref["route"]))
