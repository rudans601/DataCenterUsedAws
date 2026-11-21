"""단일 실험의 독립 HTTP 표본과 모델 출력으로 서비스 장애 탐지 성적을 집계한다."""
import json
import math

LEGACY_PROTOCOL = "single-http-v1"
PROTOCOL = "single-rag-v2"
SLO_MS = 300
ERROR_RATE = 0.05
CELLS = {"TP": ("incident", "incident"), "FN": ("incident", "normal"),
         "FP": ("normal", "incident"), "TN": ("normal", "normal")}
LABELS = {"incident": "장애", "normal": "정상", "unknown": "판단 보류"}


def ground_truth(probes):
    """실험 회차 중 한 측정 묶음이라도 서비스 기준을 위반하면 사건 단위 양성."""
    if not probes:
        return "unknown"
    valid = []
    for p in probes:
        if p["count"] < 3:
            continue
        if p["failed"] / p["count"] > ERROR_RATE:
            return "incident"
        if p["p95_ms"] is not None and math.isfinite(p["p95_ms"]):
            if p["p95_ms"] > SLO_MS:
                return "incident"
            valid.append(p)
    return "normal" if len(valid) == len(probes) else "unknown"


def assess_impact(samples, probes, unavailable, warning):
    valid = [p for p in probes if p.get("count", 0) >= 3 and p.get("failed") is not None]
    all_failed = [p["failed"] == p["count"] for p in valid]
    if unavailable and any(a and b for a, b in zip(all_failed, all_failed[1:])):
        return "critical", "연결 가능한 Pod가 없고 연속된 두 측정 구간에서 대표 요청이 모두 실패했습니다."
    if unavailable:
        return "degraded", "사용 가능한 서비스 경로가 사라졌습니다. 전면 중단 여부를 직접 호출로 확인하세요."
    if any(p["failed"] / p["count"] > ERROR_RATE or (p.get("p95_ms") or 0) > SLO_MS for p in valid):
        return "degraded", "직접 요청의 실패율 5% 또는 p95 300ms 기준을 넘은 구간이 있습니다."
    if any((m.get("error_ratio") or 0) > ERROR_RATE or (m.get("p95_ms") or 0) > SLO_MS for m in samples):
        return "degraded", "관측된 오류율이 5%를 넘거나 p95 응답 시간이 300ms를 넘었습니다."
    complete = bool(samples) and all(m.get("rps") and m.get("p95_ms") is not None
                                     and m.get("error_ratio") is not None for m in samples)
    if not complete:
        if valid and all(p["failed"] == 0 and p.get("p95_ms") is not None for p in valid):
            return "warning", "직접 요청은 성공했지만 요청 지표가 불완전합니다."
        return "unknown", "서비스 영향을 확정할 요청 지표나 독립 호출 표본이 부족합니다."
    if warning:
        return "warning", "서비스 지표는 기준 이내지만 배포 또는 클라이언트에서 이상 신호가 관측됐습니다."
    return "healthy", "요청 지표와 서비스 상태가 정상 범위입니다."


def experiment_impact(row):
    """독립 호출과 실제 주입 중 표본만 사용한다. 생성기 유형·모델 출력은 입력에서 제외한다."""
    if row.get("measurement_status") != "complete" or not row.get("injected"):
        return "unknown", "실험 측정이 완료되지 않았습니다."
    s = row["injected"]
    ready = s.get("ready") == s.get("desired") and s.get("api_code") == 200
    active_events = [e for e in s.get("pod_events", [])
                     if not (ready and e.startswith("Unhealthy Readiness probe failed: Get ")
                             and "connect: connection refused" in e)]
    warning = bool(s.get("warning_seen") or s.get("dns_error") or active_events
                   or s.get("claim_phase") == "Pending" or s.get("container_reasons")
                   or (s.get("ready", 0) < s.get("desired", 0)))
    absent = s.get("endpoints") == 0 or s.get("ready") == 0 < s.get("desired", 0)
    probes = row.get("probes", [])
    if row.get("tier_criteria") == "current-http-v1" and ground_truth(probes) == "normal":
        if absent:
            return "degraded", "서비스에 연결 가능한 Pod가 없습니다."
        missing = any(s.get(k) is None for k in ("rps", "error_ratio", "p95_ms"))
        if warning or missing:
            return "warning", "현재 직접 요청은 정상이지만 운영 경고 또는 요청 지표 공백이 있습니다."
        return "healthy", "이번 실험 구간의 독립 HTTP 요청과 서비스 상태가 정상 범위입니다."
    if (not absent and not warning and not s.get("prometheus_up") and probes
            and all(p["count"] >= 3 and p["failed"] == 0 and p.get("p95_ms") is not None
                    and p["p95_ms"] <= SLO_MS for p in probes)
            and s.get("api_code") == 200 and s.get("endpoints", 0) > 0
            and (s.get("error_ratio") or 0) <= ERROR_RATE and (s.get("p95_ms") or 0) <= SLO_MS):
        return "healthy", "독립 요청과 서비스 연결 상태가 정상 범위입니다."
    return assess_impact([s], probes, absent, warning)


def report_rows(directory):
    rows = []
    for path in sorted(directory.glob("detection-*.json"), reverse=True):
        doc = json.loads(path.read_text())
        if doc.get("protocol") not in {LEGACY_PROTOCOL, PROTOCOL}:
            continue
        for row in doc["cases"]:
            rows.append({**row, "protocol": doc["protocol"], "corpus_revision": doc.get("corpus_revision"),
                         "prompt_version": doc.get("prompt_version"), "runbook_version": doc.get("runbook_version"), "report": path.name,
                         "seed": row.get("trial_seed", doc["seed"]), "campaign_seed": doc["seed"] if doc.get("campaign_mix") else None,
                         "campaign_mix": doc.get("campaign_mix"), "started": doc["started"],
                         "model": doc["model"], "fixtures_only": doc["fixtures_only"]})
    return rows


def bucket(row):
    if row["fixtures_only"]:
        return "실측 전용"
    if row.get("measurement_status") == "interrupted":
        return "실험 중지"
    if row.get("measurement_status") != "complete":
        return "실험 시간 초과" if row.get("measurement_status") == "timeout" else "실측 미완료"
    if row.get("prediction_status") == "app_recovered":
        return "앱 1차 복구"
    if row.get("prediction_status") in ("timeout", "error"):
        return "판정 시간 초과" if row["prediction_status"] == "timeout" else "판정 오류"
    if row.get("truth") not in ("normal", "incident"):
        return "정답 미확정"
    for cell, pair in CELLS.items():
        if (row["truth"], row.get("prediction")) == pair:
            return cell
    return "판단 보류"


def decision_matrix(rows, field):
    from collections import Counter
    pairs = Counter()
    missing = 0
    prediction_key = {"impact": "model_impact", "route": "model_route", "action": "proposed_action"}[field]
    for r in rows:
        if r.get("fixtures_only") or r.get("protocol") != PROTOCOL or r.get("prediction_status") == "app_recovered":
            continue
        if field == "impact":
            expected, _ = experiment_impact(r)
        else:
            expected = (r.get("reference") or {}).get(field)
        if not expected or expected == "unknown":
            missing += 1
            continue
        predicted = r.get(prediction_key) or "미판정"
        if field == "action" and predicted in {"rollback", "scale", "restart"}:
            p = r.get("proposal") or {}
            if (p.get("namespace"), p.get("deployment")) != ("demo", "api"):
                predicted = "대상 불일치"
            elif expected == "scale" and p.get("replicas") != (r.get("injected") or {}).get("desired", 0) + 1:
                predicted = "복제본 수 불일치"
        pairs[expected, predicted] += 1
    return pairs, missing


def handoff_counts(rows):
    """최종 보류와 실제 사람 인계 티켓을 사건 단위로 짝짓는다."""
    from collections import Counter
    counts = Counter()
    for row in rows:
        if row.get("fixtures_only") or row.get("measurement_status") != "complete":
            continue
        check = row.get("recheck") or {}
        if check.get("status") == "resolved":
            counts["resolved"] += 1
        held = check.get("status") in ("held", "error") or (
            row.get("prediction_status") == "complete" and row.get("prediction") == "unknown")
        if not held:
            continue
        counts["held"] += 1
        if not isinstance(row.get("tickets"), list):
            counts["untracked"] += 1
            continue
        if row.get("incident_id") and any(t.get("id") and t.get("kind") == "사람 인계" and t.get("incident") == row["incident_id"]
               for t in row["tickets"]):
            counts["ticketed"] += 1
    return counts


def severity_statistics(rows):
    groups = {tier: [] for tier in ("healthy", "warning", "degraded", "critical", "unknown")}
    for row in rows:
        if not row.get("fixtures_only"):
            groups[experiment_impact(row)[0]].append(row)
    return {tier: (len(group), *statistics(group), handoff_counts(group)) for tier, group in groups.items()}


def statistics(rows):
    from collections import Counter
    counts = Counter(bucket(row) for row in rows)
    tp, fn, fp, tn = (counts[key] for key in CELLS)
    def ratio(a, b):
        return a / b if b else None
    decided = tp + fn + fp + tn
    return counts, {
        "재현율": ratio(tp, tp + fn), "미탐률": ratio(fn, tp + fn), "오탐률": ratio(fp, fp + tn),
        "정밀도": ratio(tp, tp + fp), "F1": ratio(2 * tp, 2 * tp + fp + fn),
        "판정 완료율": ratio(decided, len(rows) - counts["실측 전용"] - counts["앱 1차 복구"]),
    }
