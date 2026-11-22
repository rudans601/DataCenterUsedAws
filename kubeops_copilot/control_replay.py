"""EKS 운영 기록 재생. 사건 계획은 합성 기록 생성에만 사용하고 채점에는 전달하지 않는다."""
from datetime import timedelta
import random

from .detection import ground_truth
from .observations import instant

NAMES = {"api-timeout": "Kubernetes API 응답 실패", "scheduler-stall": "스케줄러 배치 중단",
         "controller-lag": "컨트롤러 반영 지연", "etcd-quota": "etcd 저장 공간 한도 초과"}
MODE = "eks-http-seeded-control-v1"


def plan_case(seed, name):
    rng = random.Random(f"control-v1/{seed}/{name}")
    c = {"onset_s": rng.choice([5, 10]), "duration_s": rng.choice([15, 45, 90]),
         "recovers": rng.choice([True, False])}
    if name == "api-timeout":
        c["sites"] = rng.choice([["운영자 PC"], ["VPC 내부"], ["운영자 PC", "VPC 내부"]])
    elif name == "scheduler-stall":
        c.update(new_pods=rng.randint(1, 3), lease_visible=rng.choice([True, False]))
    elif name == "controller-lag":
        c["replicas"] = rng.choice([[2, 3], [3, 5]])
    elif name == "etcd-quota":
        c.update(resource=rng.choice(["ConfigMap 생성", "Deployment 변경"]),
                 alarm_visible=rng.choice([True, False]), attempts=rng.randint(1, 3))
    else:
        raise ValueError(name)
    return {"case": name, "mode": MODE, "conditions": c}


def condition_rows(case):
    c = case["conditions"]
    rows = {"HTTP": "EKS 워커 실측", "Kubernetes": "시드 기반 재생",
            "발생 시점": f"+{c['onset_s']}초", "관측 시간": f"{c['duration_s']}초",
            "마지막 관측": "회복" if c["recovers"] else "지속"}
    if "sites" in c:
        rows["타임아웃 지점"] = ", ".join(c["sites"])
    if "new_pods" in c:
        rows.update({"새 Pod": str(c["new_pods"]), "리더 갱신 기록": "제공" if c["lease_visible"] else "미제공"})
    if "replicas" in c:
        rows["목표 복제본"] = " → ".join(map(str, c["replicas"]))
    if "resource" in c:
        rows.update({"쓰기 요청": c["resource"], "확인당 시도": str(c["attempts"]),
                     "알람 근거": "NOSPACE 제공" if c["alarm_visible"] else "미제공"})
    return rows


def make_window(case, incident_id, cluster, end):
    c, name = case["conditions"], case["case"]
    last = end - timedelta(seconds=8)  # 마지막 운영 조회 이후의 HTTP 표본까지 포함한다.
    start = last - timedelta(seconds=c["onset_s"] + c["duration_s"])
    onset = start + timedelta(seconds=c["onset_s"])
    middle = onset + (last - onset) / 2
    window = {"incident_id": incident_id, "cluster": cluster, "mode": MODE, "target": "demo/api",
              "phase": "EKS HTTP 실측 · Kubernetes 기록 재생", "start": start.isoformat(),
              "assess_from": onset.isoformat(), "end": end.isoformat(), "control": []}
    base = c.get("replicas", [2])[0]
    existing = [{"uid": f"api-{i}", "node": f"worker-{i%2}", "ready": True} for i in range(base)]
    if name in ("controller-lag", "scheduler-stall"):
        window["baseline_pod_uids"] = [p["uid"] for p in existing]

    for index, at in enumerate((start, onset, middle, last)):
        fault = index > 0 and (index < 3 or not c["recovers"])

        def log(stage, target, result, **details):
            window["control"].append({"at": at.isoformat(), "round": index, "stage": stage, "target": target,
                                      "result": result, "source": "seeded-kubernetes", **details})

        if name == "api-timeout":
            for site in ("운영자 PC", "VPC 내부"):
                log("read", "demo/api", "timeout" if fault and site in c["sites"] else "ok", site=site)
        elif name == "etcd-quota":
            log("read", "demo/api", "ok")
            if index:
                for _ in range(c["attempts"] if fault else 1):
                    log("write", c["resource"], "error" if fault else "ok",
                        message="NOSPACE" if fault and c["alarm_visible"] else ("쓰기 오류" if fault else "저장됨"))
                if not fault:
                    log("read", c["resource"], "ok", change_confirmed=True)
        else:
            desired = (c["replicas"][1] if name == "controller-lag" else base + c["new_pods"]) if index else base
            if index == 1:
                log("write", "demo/api scale", "ok", desired=desired)
            log("read", "demo/api", "ok")
            generation = 1 + int(index > 0)
            log("controller", "demo/api", "observed", desired=desired, generation=generation,
                observed_generation=1 if fault and name == "controller-lag" else generation)
            pods = list(existing)
            if not (fault and name == "controller-lag"):
                for i in range(desired - base):
                    pods.append({"uid": f"new-{i}", "node": None if fault else f"worker-{i%2}", "ready": not fault})
            log("placement", "demo/api", "observed", pods=pods)
            if name == "scheduler-stall" and index:
                log("read", "scheduler observations", "ok", eligible_workers=2, scheduling_events=[],
                    **({"lease_renewed_at": (start if fault else at).isoformat()} if c["lease_visible"] else {}))
    return window


def control_problem(events):
    sites = [e["site"] for e in events if e.get("site") and e["result"] == "timeout"]
    if sites:
        reason = ("양쪽 조회 시간 초과. 쓰기 가능 여부 미확인." if len(sites) == 2 else
                  f"{sites[0]} 조회 경로 점검. 다른 지점의 조회 성공.")
        return reason
    errors = [e for e in events if e["stage"] == "write" and e["result"] == "error"]
    if errors:
        return "조회 성공·쓰기 실패. " + ("NOSPACE 경고 확인." if any(e.get("message") == "NOSPACE" for e in errors)
                                   else "저장 실패 원인 미확정.")
    placements = [e for e in events if e["stage"] == "placement"]
    controllers = [e for e in events if e["stage"] == "controller"]
    if placements and controllers:
        pods = placements[-1]["pods"]
        if len(pods) < controllers[-1]["desired"]:
            return "목표 저장 후 새 Pod 생성 지연."
        if any(not p["node"] for p in pods):
            if any("Insufficient cpu" in msg for e in events for msg in e.get("scheduling_events", [])):
                return "새 Pod 생성 후 노드 미할당. 스케줄러가 CPU 부족을 보고함."
            stalled = any(e.get("eligible_workers", 0) > 0 and e.get("lease_renewed_at")
                          and instant(e["lease_renewed_at"]) < instant(e["at"]) for e in events)
            return "새 Pod 생성 후 노드 미할당. " + ("리더 갱신 정체로 스케줄러 이상 의심." if stalled else "배치 지연 원인 미확정.")
    return None


def recovery_confirmed(groups):
    final = groups[-1]
    if not final or control_problem(final):
        return False
    prior = [e for group in groups[:-1] for e in group]
    if any("site" in e for e in prior):
        return {e["site"] for e in final if e.get("site") and e["result"] == "ok"} == {"운영자 PC", "VPC 내부"}
    if any(e["stage"] == "placement" for e in prior):
        targets = [e["desired"] for e in final if e["stage"] == "controller"]
        snapshots = [e["pods"] for e in final if e["stage"] == "placement"]
        return bool(targets and snapshots and len(snapshots[-1]) == targets[-1]
                    and all(p.get("node") and p.get("ready") for p in snapshots[-1]))
    restored = {e["target"] for e in final if e["stage"] == "write" and e["result"] == "ok"}
    return any(e["target"] in restored and e["result"] == "ok" and e.get("change_confirmed") for e in final)


def grade(window, samples):
    """전체 장애 구간 최고 영향과 마지막 확인의 권장 처리 주체를 구분한다."""
    onset = instant(window["assess_from"])
    groups = [[e for e in window["control"] if e["round"] == i] for i in (1, 2, 3)]
    problems = [control_problem(group) for group in groups]
    active = [s for s in samples if instant(s["start"]) >= onset]
    last_check = instant(window["end"])
    fresh = [s for s in active if (last_check - instant(s["at"])).total_seconds() <= 15]
    http = ground_truth(active)
    impact = {"normal": "warning" if any(problems) else "healthy", "incident": "degraded", "unknown": "unknown"}[http]
    endpoint = window.get("eks_endpoints")
    unavailable = bool(endpoint and endpoint["source"] == "eks-endpointslices" and endpoint["ready"] == 0
                       and onset <= instant(endpoint["at"]) <= last_check)
    if unavailable and http != "unknown":
        impact = "degraded"
        observed = instant(endpoint["at"])
        near = [s for s in active if 0 <= (observed - instant(s["at"])).total_seconds() <= 20]
        if len(near) >= 2 and all(s["failed"] == s["count"] for s in near[-2:]):
            impact = "critical"
    if not fresh and impact in ("healthy", "warning"):
        impact = "unknown"
    last_round = max((instant(e["at"]) for e in groups[-1]), default=last_check)
    followup = [s for s in fresh if instant(s["start"]) >= last_round]
    recovered = not unavailable and recovery_confirmed(groups) and ground_truth(followup) == "normal"
    route = "observe" if recovered else "handoff"
    reason = problems[-1] or ("마지막 운영 확인과 앱 요청 회복." if recovered else "회복 근거 부족 또는 앱 장애 지속.")
    return {"impact": impact, "route": route, "action": "none" if recovered else "manual",
            "reason": reason, "initial_finding": next((p for p in problems if p), None), "http": http}
