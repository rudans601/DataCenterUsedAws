"""관측한 서비스 영향과 변경을 담당할 주체를 각각 결정한다."""

import datetime as dt

from .detection import ERROR_RATE, SLO_MS, assess_impact, experiment_impact  # 기존 호출 호환

ROUTES = {"observe": "자동 관찰", "auto": "제한 자동 조치", "candidate": "이전 버전의 조치 후보",
          "approval": "운영자 승인 후 에이전트 실행", "handoff": "사람이 직접 처리"}
IMPACTS = {"healthy": "정상", "warning": "주의", "degraded": "장애", "critical": "중대 장애", "unknown": "판단 보류"}


AUTO_PROCEDURES = {
    "scale": "high-load-scaling.md", "rollback": "revision-recovery.md",
}


def auto_block(snapshot, proposal, analysis, local):
    """모델이 선택한 자동 변경의 물리적 전제만 검사한다. 장애 유형은 입력으로 쓰지 않는다."""
    action = proposal["action"]
    target = (proposal["namespace"], proposal["deployment"])
    if not local or target != ("demo", "api") or action not in AUTO_PROCEDURES:
        return "자동 변경은 로컬 demo/api의 제한 증설·롤백 범위에서만 허용됩니다."
    filename = AUTO_PROCEDURES[action]
    if (not (analysis.get("citation_source") or "").endswith("/" + filename)
            or f"auto.{action}" not in analysis.get("cited_procedures", []) or analysis.get("confidence") != "high"
            or analysis.get("service_detection") not in {"normal", "incident"}
            or analysis.get("impact") in ("critical", "unknown") or analysis.get("proposal", {}).get("requested_by_user")):
        return "자동 절차의 원문, 높은 확신도와 적용 가능한 서비스 영향이 필요합니다."
    dep = next((d for d in snapshot["deployments"] if d["name"] == "api"), {})
    svc = next((s for s in snapshot["services"] if s["name"] == "api"), {})
    m = snapshot.get("metrics", {}).get("api", {})
    facts = snapshot.get("recovery_facts", {})
    desired = dep.get("desired", 0)
    if (not desired or not svc.get("ready_endpoints") or dep.get("observed_generation") != dep.get("generation")
            or (m.get("rps") or 0) <= 0 or m.get("error_ratio") is None or m.get("p95_ms") is None):
        return "서비스 요청·엔드포인트 또는 대상 세대의 관측이 불충분합니다."
    last = dep.get("last_auto_at")
    if last:
        try:
            if (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds() < 600:
                return "최근 10분 이내 같은 대상에 자동 변경이 있었습니다."
        except (TypeError, ValueError):
            return "이전 자동 변경 시각을 확인할 수 없습니다."
    if action == "scale":
        if (proposal.get("replicas") != desired + 1 or dep.get("ready") != desired
                or dep.get("available") != desired or dep.get("updated") != desired
                or not facts.get("spare_worker") or (facts.get("previous_p95_ms") or 0) <= SLO_MS
                or m["p95_ms"] <= SLO_MS or m["error_ratio"] > ERROR_RATE
                or not ((m.get("cpu_util") or 0) >= .85 or (m.get("throttle_ratio") or 0) >= .25)):
            return "지속적인 지연·CPU 포화, 안정된 배포, 여유 워커와 복제본 +1 조건을 충족하지 않습니다."
    elif (not facts.get("only_args_or_readiness") or not facts.get("new_pods") or facts.get("new_ready") != 0
          or facts.get("previous_available", 0) < desired or m["p95_ms"] > SLO_MS or m["error_ratio"] > ERROR_RATE
          or not any(p.get("revision") == str(dep.get("revision")) and not p.get("terminating")
                     and not p.get("ready") and (p.get("restarts", 0) > 0 or any(
                         e.get("object") == p["name"] and e.get("type") == "Warning" for e in snapshot.get("events", [])))
                     for p in snapshot.get("pods", []))):
        return "새 리비전의 실제 실패, 기존 가용 복제본과 제한된 템플릿 차이를 확인할 수 없습니다."
    return None


def classify(snapshot, analysis, guard, *, local=False):
    requested = analysis.get("route", "handoff")
    reason = analysis.get("decision_reason") or "판단 근거가 부족합니다."
    route = requested
    p = guard["normalized"]
    block = None
    if guard["executable"] and not guard["allowed"]:
        block = " ".join(guard["reasons"])
    elif requested == "observe" and not analysis.get("citation_source"):
        block = "관찰로 종료할 근거를 현행 런북 원문에서 확인하지 못했습니다."
    elif requested in ("approval", "auto"):
        if (not guard["executable"] or not analysis.get("citation_source") or not analysis.get("target_grounded")
                or analysis.get("confidence") == "low" or analysis.get("impact") == "unknown"):
            block = "변경 대상의 관측 근거와 유효한 런북 절차 또는 확신도가 부족합니다."
        elif f"{requested}.{p['action']}" not in analysis.get("cited_procedures", []):
            block = "인용한 런북 원문에 제안한 경로와 조치를 허용하는 실행 절차가 없습니다."
        elif any(s["name"] == p["deployment"] and s.get("selector") and s.get("matching_pods") == 0
                 and any(d["name"] == s["name"] and d.get("ready", 0) > 0 for d in snapshot["deployments"])
                 for s in snapshot.get("services", [])):
            block = "Ready Pod와 Service selector가 일치하지 않아 해당 Deployment 변경은 인계합니다."
        elif not guard.get("target_version") or guard["target_version"] != observed_version(snapshot, p):
            block = "관측 후 Deployment가 변경됐거나 해당 대상을 관측하지 못했습니다."
        elif requested == "auto":
            block = auto_block(snapshot, p, analysis, local)
    elif requested == "observe" and (p["action"] != "none" or analysis.get("impact") in ("critical", "unknown")):
        block = "관찰 제안과 조치 또는 서비스 영향이 일치하지 않아 확인이 필요합니다."
    if block:
        route, reason = "handoff", block
    return {"impact": analysis.get("impact", "unknown"), "route": route, "model_route": requested,
            "reason": reason, "impact_reason": analysis.get("decision_reason", ""), "blocked": bool(block)}


def observed_version(snapshot, proposal):
    if proposal.get("namespace") == snapshot.get("namespace"):
        for dep in snapshot.get("deployments", []):
            if dep["name"] == proposal["deployment"]:
                return {"uid": dep["uid"], "generation": dep["generation"], "revision": int(dep["revision"] or 0)}
    return None
