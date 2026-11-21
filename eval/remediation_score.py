"""맹검 평가 채점기. 주입 및 독립 측정이 끝나면 추론 전에 비교 기준을 기록한다."""
from datetime import datetime

from kubeops_copilot.detection import ground_truth, SLO_MS, ERROR_RATE


def reference_decision(case, row, snapshot):
    name = case["case"]
    sample = row["injected"]
    m = snapshot.get("metrics", {}).get("api", {})
    f = snapshot.get("recovery_facts", {})
    desired = sample.get("desired", 0)
    label = row["truth"]

    def ref(route, action, reason):
        return {"route": route, "action": action, "reason": reason}

    if label == "unknown":
        return ref(None, None, "독립 HTTP 표본 부족")
    if name in {"normal", "transient-warning", "maintenance"}:
        return (ref("observe", "none", "독립 요청 정상, 정상 대조 또는 일시 신호") if label == "normal"
                else ref(None, None, "대조 조건에서 예상 밖 서비스 영향 발생"))
    if name in {"dns", "pvc", "pending", "service-broken"}:
        return ref("handoff", "manual", "실제 확인한 연결·배치·저장소 문제의 수정은 운영자 권한")
    if name == "metrics-gap":
        if snapshot.get("observations", {}).get("persistent_gap"):
            return ref("handoff", "manual", "연속된 수집 구간의 공백 확인")
        return ref(None, None, "수집 공백 지속 시간에 대한 기준 표본 부족")
    last_auto = f.get("last_auto_at")
    if last_auto and name in {"high-load", "crashloop", "oom", "probe-fail"}:
        measured = row["episodes"][-1]["measured_until"]
        if (datetime.fromisoformat(measured) - datetime.fromisoformat(last_auto)).total_seconds() < 600:
            return ref(None, None, "최근 자동 변경의 대기 구간으로 다음 변경의 적절성은 별도 검토")
    if case["conditions"]["visibility"] == "no_request_metrics" and name in {"high-load", "crashloop", "oom", "probe-fail"}:
        return ref(None, None, "요청 지표를 가린 조건에서 자동 변경의 근거가 충분한지 별도 검토")
    if name == "high-load":
        if case["conditions"]["pattern"] == "burst" or label == "normal":
            return ref("observe", "none", "짧은 부하 종료 또는 서비스 SLO 충족")
        if (f.get("spare_worker") and (f.get("previous_p95_ms") or 0) > SLO_MS
                and (m.get("p95_ms") or 0) > SLO_MS and m.get("error_ratio") is not None
                and m["error_ratio"] <= ERROR_RATE and desired and desired < 6 and sample["ready"] == desired
                and ((m.get("cpu_util") or 0) >= .85 or (m.get("throttle_ratio") or 0) >= .25)):
            return ref("auto", "scale", "지속 포화·여유 워커·복제본 +1")
        return ref(None, None, "관찰·승인 증설을 구분할 충분한 참조 조건 없음")
    if name == "bad-deploy":
        if label != "incident":
            return ref(None, None, "주입한 지연의 서비스 영향 미확인")
        return ref("approval", "rollback", "트래픽을 받는 새 버전의 지연을 직접 확인")
    if name in {"crashloop", "oom", "probe-fail", "image-pull"}:
        if not f.get("previous_revision"):
            return ref(None, None, "직전 리비전 확인 불가")
        if (f.get("only_args_or_readiness") and f.get("new_pods") and f.get("new_ready") == 0
                and desired and f.get("previous_available", 0) >= desired and label == "normal"):
            return ref("auto", "rollback", "새 버전만 실패하고 직전 버전으로 정상 요청을 처리")
        return ref("approval", "rollback", "배포 전 정상 대조와 기존 리비전 확인, 자동 변경 범위 밖")
    return ref(None, None, "독립 채점 기준 미확정")


def recovery_outcome(row):
    """정리 전, 에이전트의 실제 변경과 회복 근거가 모두 있는 사례만 성공이 될 수 있다."""
    result = row.get("action_result")
    if not result:
        return "미실행"
    if not result.get("ok"):
        return "실행 실패"
    verdict = row.get("verification", {}).get("verdict")
    if verdict == "failed":
        return "복구 실패"
    if verdict == "partial":
        return "부분 개선"
    if verdict != "success" or ground_truth(row.get("after_action_probes", [])) == "unknown":
        return "검증 불가"
    if ground_truth(row["after_action_probes"]) != "normal":
        return "복구 실패"
    if row["case"] == "high-load" and not row.get("verification_load_active"):
        return "검증 불가"
    before_http = ground_truth(row.get("pre_action_probes", []))
    if before_http == "unknown":
        return "검증 불가"
    if before_http == "incident":
        return "서비스 복구"
    before = row.get("before_action", {})
    if before.get("problem_pods", 0) > 0 or before.get("ready", 0) < before.get("desired", 0):
        return "배포 복구"
    return "개선 근거 부족"
