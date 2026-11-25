"""정상 HTTP 관측을 당시 Deployment/ReplicaSet에 연결한다. 조치 정답은 생성하지 않는다."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json

VERSION = "baseline-revision-v1"


def template_hash(obj):
    template = deepcopy(obj["spec"]["template"])
    template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
    return hashlib.sha256(json.dumps(template, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(obj):
    m = obj.get("metadata", {})
    return {"uid": m.get("uid"), "generation": m.get("generation"),
            "revision": int(m.get("annotations", {}).get("deployment.kubernetes.io/revision", 0))}


def owned_sets(deployment, sets):
    uid = deployment.get("metadata", {}).get("uid")
    return [rs for rs in sets if uid and any(ref.get("kind") == "Deployment" and ref.get("uid") == uid
                                            for ref in rs.get("metadata", {}).get("ownerReferences", []))]


def baseline_record(before, after, sets, sample, started_at, finished_at):
    """HTTP 수집 전후 같은 배포임을 확인한 경우에만 리비전과 관측을 연결한다."""
    target = identity(after)
    desired = after.get("spec", {}).get("replicas", 0)
    candidates = [rs for rs in owned_sets(after, sets) if identity(rs)["revision"] == target["revision"]]
    rs = candidates[0] if len(candidates) == 1 else None
    issues = []
    if not target["uid"] or not target["revision"] or identity(before) != target:
        issues.append("HTTP 관측 전후 Deployment 식별자가 일치하지 않음")
    try:
        observed = datetime.fromisoformat(sample["api_observed_at"])
        if not datetime.fromisoformat(started_at) <= observed <= datetime.fromisoformat(finished_at):
            issues.append("HTTP 관측 시각이 Deployment 확인 구간 밖임")
    except (KeyError, TypeError, ValueError):
        issues.append("HTTP 관측 시각 없음")
    if (not desired or sample.get("api_code") != 200 or sample.get("ready") != desired
            or sample.get("desired") != desired or not sample.get("endpoints")):
        issues.append("정상 HTTP·Ready·엔드포인트 조건 미충족")
    if (not rs or template_hash(before) != template_hash(after) or template_hash(rs) != template_hash(after)
            or (rs.get("status", {}).get("readyReplicas", 0) < desired)
            or (rs.get("status", {}).get("availableReplicas", 0) < desired)
            or after.get("status", {}).get("observedGeneration") != target["generation"]):
        issues.append("동일 템플릿의 현재 ReplicaSet 가용 상태 미확인")
    return {"version": VERSION, "namespace": "demo", "deployment": "api", **target,
            "strategy": after.get("spec", {}).get("strategy", {}).get("type"),
            "template_sha256": template_hash(after), "started_at": started_at, "finished_at": finished_at,
            "http": {k: sample.get(k) for k in ("api_observed_at", "api_code", "api_ms", "ready", "desired", "endpoints")},
            "replicaset": rs.get("metadata", {}).get("name") if rs else None,
            "matched": not issues, "issues": issues}


def recovery_evidence(baseline, current, sets, checked_at):
    """당시 정상 리비전이 지금도 같은 소유자의 직전 템플릿인지 확인한다."""
    target = identity(current)
    prior = [rs for rs in owned_sets(current, sets) if 0 < identity(rs)["revision"] < target["revision"]]
    previous = max(prior, key=lambda rs: identity(rs)["revision"], default=None)
    matched = bool(baseline["matched"] and target["uid"] == baseline["uid"] and previous
                   and identity(previous)["revision"] == baseline["revision"]
                   and template_hash(previous) == baseline["template_sha256"])
    payload = {"baseline": baseline, "current": {**target, "checked_at": checked_at,
               "strategy": current.get("spec", {}).get("strategy", {}).get("type")},
               "previous_template_matches_baseline": matched,
               "previous_available_now": previous.get("status", {}).get("availableReplicas", 0) if previous else None}
    return {"source": "history", "target": "demo/api", "severity": "info",
            "title": "장애 전 정상 HTTP의 리비전 기록과 현재 직전 템플릿 대조",
            "detail": json.dumps(payload, ensure_ascii=False)}
