"""Kubernetes 조회 도구 (읽기 전용).

조회 결과는 그대로 LLM에 넘기지 않고, 짧게 정리한 '근거(Evidence)'로 만든다.
근거마다 E1, E2 ... 번호를 붙여 답변이 어떤 근거를 인용했는지 화면과 평가에서 확인할 수 있게 한다.
"""
from __future__ import annotations

import datetime as dt
import re
import os
import time

import requests
from dataclasses import asdict, dataclass
from functools import lru_cache

from kubernetes import client
from kubernetes import config as kconfig
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from . import metrics
from .config import settings

PROBLEM_REASONS = {
    "CrashLoopBackOff", "OOMKilled", "Error", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerConfigError", "RunContainerError", "ContainerCannotRun",
}
_LOG_PATTERN = re.compile(
    r"(error|fatal|panic|fail|exception|unknown flag|oom|refused|timeout|denied)", re.I
)


# ---------------------------------------------------------------- 근거 기록
@dataclass
class Evidence:
    id: str
    source: str  # deployment | pods | events | logs | rollout | service | metrics
    target: str
    title: str
    detail: str
    severity: str = "info"  # info | warning | critical

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceBook:
    def __init__(self) -> None:
        self.items: list[Evidence] = []

    def add(self, source: str, target: str, title: str, detail: str, severity: str = "info") -> Evidence:
        ev = Evidence(f"E{len(self.items) + 1}", source, target, title, detail.strip(), severity)
        self.items.append(ev)
        return ev


# ---------------------------------------------------------------- 클라이언트
@lru_cache(maxsize=1)
def api_client() -> client.ApiClient:
    return kconfig.new_client_from_config(config_file=settings.kubeconfig)


def reset_client() -> None:
    api_client.cache_clear()


def core() -> client.CoreV1Api:
    return client.CoreV1Api(api_client())


def apps() -> client.AppsV1Api:
    return client.AppsV1Api(api_client())


def discovery() -> client.DiscoveryV1Api:
    return client.DiscoveryV1Api(api_client())


# ---------------------------------------------------------------- 공통 유틸
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def age(ts: dt.datetime | None) -> str:
    if not ts:
        return "-"
    sec = int((_now() - ts).total_seconds())
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _iso(ts: dt.datetime | None) -> str:
    return ts.isoformat() if ts else ""


def _matches(selector: dict, labels: dict | None) -> bool:
    labels = labels or {}
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _fmt_args(args: list[str]) -> str:
    return " ".join(args) if args else "(기본값)"


# ---------------------------------------------------------------- 조회 함수
def list_deployments(ns: str) -> list[dict]:
    rows = []
    for d in apps().list_namespaced_deployment(ns).items:
        c = d.spec.template.spec.containers[0]
        ann = d.metadata.annotations or {}
        conds = {x.type: x for x in (d.status.conditions or [])}
        prog = conds.get("Progressing")
        probe = c.readiness_probe.http_get.path if c.readiness_probe and c.readiness_probe.http_get else ""
        rows.append({
            "name": d.metadata.name, "uid": d.metadata.uid,
            "last_auto_at": ann.get("kubeops.local/last-auto-at"),
            "desired": d.spec.replicas or 0,
            "ready": d.status.ready_replicas or 0,
            "updated": d.status.updated_replicas or 0,
            "available": d.status.available_replicas or 0,
            "revision": ann.get("deployment.kubernetes.io/revision", ""),
            "change_cause": ann.get("kubernetes.io/change-cause", ""),
            "image": c.image,
            "args": list(c.args or []),
            "cpu_limit": (c.resources.limits or {}).get("cpu", ""),
            "memory_limit": (c.resources.limits or {}).get("memory", ""),
            "readiness_path": probe,
            "progressing": f"{prog.status}/{prog.reason}" if prog else "",
            "progress_message": (prog.message or "") if prog else "",
            "selector": dict(d.spec.selector.match_labels or {}),
            "generation": d.metadata.generation or 0,
            "observed_generation": d.status.observed_generation or 0,
        })
    return rows


def _replicaset_revisions(ns: str) -> dict[str, str]:
    out = {}
    for rs in apps().list_namespaced_replica_set(ns).items:
        out[rs.metadata.name] = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
    return out


def list_pods(ns: str) -> list[dict]:
    revisions = _replicaset_revisions(ns)
    rows = []
    for p in core().list_namespaced_pod(ns).items:
        cs = (p.status.container_statuses or [None])[0]
        state, reason, last, restarts, ready = "Pending", "", "", 0, False
        if cs is not None:
            restarts, ready = cs.restart_count or 0, bool(cs.ready)
            if cs.state.waiting:
                state, reason = "Waiting", cs.state.waiting.reason or ""
            elif cs.state.running:
                state = "Running"
            elif cs.state.terminated:
                state, reason = "Terminated", cs.state.terminated.reason or ""
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                last = f"{t.reason} (exit {t.exit_code})"
        owner = next((o.name for o in (p.metadata.owner_references or []) if o.kind == "ReplicaSet"), "")
        rows.append({
            "name": p.metadata.name,
            "labels": dict(p.metadata.labels or {}),
            "phase": p.status.phase,
            "ready": ready,
            "state": state,
            "reason": reason,
            "restarts": restarts,
            "last_termination": last,
            "node": p.spec.node_name or "",
            "age": age(p.metadata.creation_timestamp),
            "revision": revisions.get(owner, "?"),
            "terminating": p.metadata.deletion_timestamp is not None,
        })
    return rows


def recent_events(ns: str, minutes: int = 15) -> list[dict]:
    now = _now()
    rows = []
    for e in core().list_namespaced_event(ns).items:
        ts = e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp
        if ts and (now - ts).total_seconds() > minutes * 60:
            continue
        rows.append({
            "type": e.type or "Normal",
            "reason": e.reason or "",
            "kind": e.involved_object.kind,
            "object": e.involved_object.name,
            "message": (e.message or "").replace("\n", " ")[:240],
            "count": e.count or 1,
            "age": age(ts),
            "_ts": ts.timestamp() if ts else 0.0,
        })
    rows.sort(key=lambda r: (r["type"] != "Warning", -r["_ts"]))
    seen, out = set(), []
    for r in rows:
        key = (r["type"], r["reason"], re.sub(r"[a-z0-9]{5,10}-[a-z0-9]{5}", "*", r["object"]), r["message"][:80])
        if key in seen:
            continue
        seen.add(key)
        r["last_seen_epoch"] = r.pop("_ts")
        out.append(r)
    return out


def pod_logs(ns: str, pod: str, previous: bool = False, tail: int = 80, keep: int = 10) -> str:
    try:
        text = core().read_namespaced_pod_log(pod, ns, tail_lines=tail, previous=previous)
    except ApiException as e:
        return f"(로그를 가져오지 못함: {e.status} {e.reason})"
    if not text:
        return "(로그 없음)"
    lines = [ln[:300] for ln in text.splitlines() if ln.strip()]
    errors = [ln for ln in lines if _LOG_PATTERN.search(ln)]
    picked: list[str] = []
    for ln in errors[-8:] + lines[-3:]:
        if ln not in picked:
            picked.append(ln)
    return "\n".join(picked[-keep:]) or "(로그 없음)"


def rollout_history(ns: str, deployment: str) -> list[dict]:
    rows = []
    for rs in apps().list_namespaced_replica_set(ns).items:
        if not any(o.kind == "Deployment" and o.name == deployment for o in (rs.metadata.owner_references or [])):
            continue
        ann = rs.metadata.annotations or {}
        c = rs.spec.template.spec.containers[0]
        probe = c.readiness_probe.http_get.path if c.readiness_probe and c.readiness_probe.http_get else ""
        rows.append({
            "revision": int(ann.get("deployment.kubernetes.io/revision", "0") or 0),
            "replicaset": rs.metadata.name,
            "change_cause": ann.get("kubernetes.io/change-cause", ""),
            "image": c.image,
            "args": list(c.args or []),
            "memory_limit": (c.resources.limits or {}).get("memory", ""),
            "cpu_limit": (c.resources.limits or {}).get("cpu", ""),
            "readiness_path": probe,
            "replicas": rs.status.replicas or 0,
            "ready": rs.status.ready_replicas or 0,
            "created": _iso(rs.metadata.creation_timestamp),
            "age": age(rs.metadata.creation_timestamp),
        })
    rows.sort(key=lambda r: r["revision"], reverse=True)
    return rows


def service_status(ns: str, pods: list[dict] | None = None) -> list[dict]:
    pods = pods if pods is not None else list_pods(ns)
    ready_by_svc: dict[str, int] = {}
    for s in discovery().list_namespaced_endpoint_slice(ns).items:
        name = (s.metadata.labels or {}).get("kubernetes.io/service-name", "")
        n = sum(1 for ep in (s.endpoints or []) if ep.conditions is None or ep.conditions.ready is not False)
        ready_by_svc[name] = ready_by_svc.get(name, 0) + n
    rows = []
    for svc in core().list_namespaced_service(ns).items:
        sel = dict(svc.spec.selector or {})
        matching = [p for p in pods if _matches(sel, p["labels"]) and not p["terminating"]]
        rows.append({
            "name": svc.metadata.name,
            "type": svc.spec.type,
            "selector": sel,
            "ports": [f"{p.port}->{p.target_port}" for p in (svc.spec.ports or [])],
            "matching_pods": len(matching),
            "ready_endpoints": ready_by_svc.get(svc.metadata.name, 0),
        })
    return rows


# ---------------------------------------------------------------- 근거 수집
def _diff(newer: dict, older: dict) -> list[str]:
    changes = []
    for key, label in [("image", "이미지"), ("args", "실행 인자"), ("memory_limit", "메모리 limit"),
                       ("cpu_limit", "CPU limit"), ("readiness_path", "readinessProbe 경로")]:
        if newer.get(key) != older.get(key):
            a, b = older.get(key), newer.get(key)
            if key == "args":
                added = [x for x in b if x not in a]
                removed = [x for x in a if x not in b]
                changes.append(f"{label} 추가 {added} 제거 {removed}")
            else:
                changes.append(f"{label} {a} → {b}")
    return changes


def collect_evidence(ns: str, targets: list[str] | None, book: EvidenceBook) -> dict:
    """대상 네임스페이스의 상태를 모아 근거를 만든다. 화면용 원본 데이터도 함께 돌려준다."""
    deps = list_deployments(ns)
    pods = list_pods(ns)
    services = service_status(ns, pods)
    existing = {p["name"]: p for p in pods}

    def _relevant(e: dict) -> bool:
        if e["kind"] != "Pod":
            return True
        p = existing.get(e["object"])
        if p is None:  # 이미 사라진 Pod의 이벤트
            return False
        # 기동 직후 잠깐 난 Probe 실패는 지금 Ready면 무시한다.
        return not (e["reason"] == "Unhealthy" and p["ready"] and e["count"] <= 3)

    events = [e for e in recent_events(ns) if _relevant(e)]
    names = [d["name"] for d in deps]
    focus = [t for t in (targets or []) if t in names] or names
    signals: set[str] = set()
    metric_rows: dict[str, dict] = {}

    for d in deps:
        if d["name"] not in focus:
            continue
        name = d["name"]
        target = f"{ns}/{name}"
        dpods = [p for p in pods if _matches(d["selector"], p["labels"])]

        # 1) Deployment 상태
        sev = "info"
        if d["available"] < d["desired"] or d["ready"] == 0:
            sev = "critical"
        elif d["updated"] < d["desired"] or "ProgressDeadlineExceeded" in d["progressing"]:
            sev = "warning"
        if "ProgressDeadlineExceeded" in d["progressing"]:
            signals.add("rollout stuck ProgressDeadlineExceeded")
        book.add("deployment", target, f"Deployment {name} 상태", f"""
원하는 복제본 {d['desired']}, Ready {d['ready']}, 최신 리비전으로 갱신 {d['updated']}, Available {d['available']}
현재 리비전 {d['revision']} (change-cause: {d['change_cause'] or '-'})
롤아웃 상태: {d['progressing']} {d['progress_message']}
리소스 limit: cpu={d['cpu_limit']}, memory={d['memory_limit']} / readinessProbe 경로: {d['readiness_path']}
실행 인자: {_fmt_args(d['args'])}""", sev)

        # 2) Pod 상태
        lines, psev = [], "info"
        for p in dpods:
            flag = p["reason"] or p["state"]
            lines.append(
                f"{p['name']} rev{p['revision']} {flag} ready={p['ready']} restarts={p['restarts']}"
                f"{' last=' + p['last_termination'] if p['last_termination'] else ''} node={p['node']} age={p['age']}"
                f"{' (종료 중)' if p['terminating'] else ''}"
            )
            for r in (p["reason"], p["last_termination"].split(" ")[0] if p["last_termination"] else ""):
                if r in PROBLEM_REASONS:
                    psev = "critical"
                    signals.add(r)
            if not p["ready"] and not p["terminating"] and psev == "info":
                psev = "warning"
                signals.add("pod not ready")
        book.add("pods", target, f"{name} Pod {len(dpods)}개", "\n".join(lines) or "Pod 없음", psev)

        # 3) 이벤트
        related = [e for e in events if e["object"] == name or e["object"].startswith(name + "-")]
        if related:
            esev = "warning" if any(e["type"] == "Warning" for e in related) else "info"
            for e in related:
                if e["type"] == "Warning":
                    signals.add(e["reason"])
                    if "probe failed" in e["message"].lower():
                        signals.add("probe failed")
            book.add("events", target, f"{name} 최근 이벤트", "\n".join(
                f"[{e['type']}] {e['reason']} {e['kind']}/{e['object']} x{e['count']} ({e['age']} 전): {e['message']}"
                for e in related[:8]
            ), esev)

        # 4) 문제 Pod 로그 (최대 2개)
        troubled = [p for p in dpods if (p["restarts"] > 0 or not p["ready"]) and not p["terminating"]][:2]
        for p in troubled:
            previous = p["restarts"] > 0
            book.add("logs", target, f"{p['name']} 로그{' (재시작 전 컨테이너)' if previous else ''}",
                     pod_logs(ns, p["name"], previous=previous), "warning")

        # 5) 배포 이력
        hist = rollout_history(ns, name)[:3]
        if hist:
            lines = []
            for i, h in enumerate(hist):
                line = (f"rev {h['revision']} ({h['age']} 전 생성) {h['change_cause'] or '-'} | "
                        f"Pod ready {h['ready']}/{h['replicas']}")
                if i + 1 < len(hist):
                    changes = _diff(h, hist[i + 1])
                    if changes:
                        line += f" | rev {hist[i + 1]['revision']} 대비 변경: " + "; ".join(changes)
                lines.append(line)
            book.add("rollout", target, f"{name} 배포 이력 (최근 {len(hist)}개)", "\n".join(lines))

        # 6) 메트릭
        m = metrics.deployment_metrics(ns, name)
        metric_rows[name] = m
        if m.get("available"):
            msev = "info"
            notes = []
            if (m.get("error_ratio") or 0) > 0.05:
                msev = "critical" if m["error_ratio"] > 0.2 else "warning"
                notes.append("오류율 높음")
                signals.add("high error rate 5xx")
            if (m.get("p95_ms") or 0) > 300:
                msev = "warning" if msev == "info" else msev
                notes.append("응답 지연")
                signals.add("high latency")
            if (m.get("throttle_ratio") or 0) > 0.25 or (m.get("cpu_util") or 0) > 0.85:
                msev = "warning" if msev == "info" else msev
                notes.append("CPU 포화")
                signals.add("CPU throttling saturation")
            if (m.get("mem_util") or 0) > 0.85:
                notes.append("메모리 limit 근접")
                signals.add("memory near limit")
            book.add("metrics", target, f"{name} 메트릭 (최근 {m['window']})", metrics.describe(m)
                     + (f"\n판단 메모: {', '.join(notes)}" if notes else ""), msev)

    # 7) Service ↔ Pod 연결
    for s in services:
        related = s["name"] in focus or any(_matches(s["selector"], {"app": f}) for f in focus)
        broken = s["ready_endpoints"] == 0 and s["type"] != "ExternalName"
        if not (related or broken):
            continue
        detail = (f"type={s['type']} selector={s['selector']} ports={s['ports']}\n"
                  f"selector와 일치하는 Pod {s['matching_pods']}개, Ready 엔드포인트 {s['ready_endpoints']}개")
        same = next((d for d in deps if d["name"] == s["name"]), None)
        if broken and same:
            detail += f"\n참고: 같은 이름의 Deployment {same['name']}의 Pod 레이블 selector는 {same['selector']}"
        if broken:
            signals.add("service has no ready endpoints")
            if same and same["ready"] > 0 and s["selector"] and s["matching_pods"] == 0:
                signals.add("service selector mismatch")
        book.add("service", f"{ns}/{s['name']}", f"Service {s['name']} 연결 상태", detail,
                 "critical" if broken else "info")

    observations = {}
    url = os.getenv("KUBEOPS_DEMO_API_PROBE_URL")
    if url and ns == "demo" and focus == ["api"]:
        observations["probes"] = api_probe_windows(url)
        book.add("probe", "demo/api", "대표 서비스 HTTP 직접 호출", str(observations["probes"]))
    if ns == "demo" and "api" in focus:
        sample = metric_rows.get("api", {})
        if sample.get("rps") is None and sample.get("p95_ms") is None and metrics.is_up():
            # 이전 구간에서도 API 시계열이 없었는지 모니터링 서버 자체의 가용성과 함께 확인한다.
            query = ('absent_over_time(http_request_duration_seconds_count{namespace="demo",app="api"}[2m] offset 2m)'
                     ' and on() (min_over_time(up{job="prometheus"}[4m]) == 1)')
            observations["persistent_gap"] = metrics.scalar(query) == 1
            book.add("metrics", "demo/api", "요청 지표 수집 공백", f"직전 2분 구간에도 공백 확인={observations['persistent_gap']}")
    recovery_facts = {}
    if ns == "demo" and "api" in focus:
        from . import actions
        dep = apps().read_namespaced_deployment("api", ns)
        recovery_facts = {**scale_evidence(),
                          **actions.rollback_facts(dep, apps().list_namespaced_replica_set(ns).items),
                          "last_auto_at": (dep.metadata.annotations or {}).get("kubeops.local/last-auto-at")}
        book.add("capacity", "demo/api", "직전 리비전 가용 수·현재 배치 여유",
                 str({k: v for k, v in recovery_facts.items() if k != "previous_p95_ms"}))
        book.add("history", "demo/api", "이전 비교 구간 · 수집 4분 전부터 2분 전까지의 p95(ms)",
                 str(recovery_facts.get("previous_p95_ms")))
    return {
        "namespace": ns, "observations": observations, "recovery_facts": recovery_facts,
        "deployments": deps,
        "pods": [{k: v for k, v in p.items() if k != "labels"} for p in pods],
        "services": services,
        "events": events[:15],
        "metrics": metric_rows,
        "signals": sorted(signals),
    }


def api_probe_windows(url):
    """운영자가 지정한 demo/api의 읽기 전용 서비스 경로에서 독립 HTTP 표본을 수집한다."""
    windows = []
    for _ in range(2):
        failed = 0
        latencies = []
        for _ in range(3):
            start = time.monotonic()
            try:
                response = requests.get(url, timeout=(0.6, 1), allow_redirects=False, stream=True)
                with response:
                    failed += int(not 200 <= response.status_code < 300)
            except requests.RequestException:
                failed += 1
            latencies.append(1000 * (time.monotonic() - start))
        windows.append({"count": 3, "failed": failed, "p95_ms": round(max(latencies), 2)})
    return windows


def scale_evidence():
    """demo/api 전용. 최근 두 구간 지연과 복제본 하나의 CPU·메모리 요청 여유를 검토한다."""
    selector = 'namespace="demo",app="api",path!~"healthz|readyz|metrics"'
    previous = metrics.scalar('histogram_quantile(0.95, sum by (le) (rate('
                              f'http_request_duration_seconds_bucket{{{selector}}}[2m] offset 2m))) * 1000')
    context = {"previous_p95_ms": previous, "spare_worker": None}
    if previous is None or previous <= 300:
        return context
    try:
        pod = apps().read_namespaced_deployment("api", "demo").spec.template.spec
    except ApiException:
        return context
    if (pod.node_selector or pod.affinity or pod.node_name or pod.init_containers or pod.topology_spread_constraints
            or len(pod.containers) != 1 or any(v.persistent_volume_claim for v in pod.volumes or [])):
        return context
    resource_requests = getattr(pod.containers[0].resources, "requests", None) or {}
    if not all(key in resource_requests for key in ("cpu", "memory")) or set(resource_requests) - {"cpu", "memory"}:
        return context
    selectors = []
    for resource in ("cpu", "memory"):
        needed = float(parse_quantity(resource_requests[resource]))
        if needed <= 0:
            return context
        alloc = f'max by (node) (kube_node_status_allocatable{{resource="{resource}"}})'
        used = f'sum by (node) (kube_pod_container_resource_requests{{resource="{resource}",node!=""}})'
        selectors.append(f'(({alloc} - on(node) {used}) >= {needed})')
    selectors.extend(['(kube_node_status_condition{condition="Ready",status="true"} == 1)',
                      '(kube_node_spec_unschedulable == 0)'])
    query = " and on(node) ".join(selectors) + ' unless on(node) kube_node_spec_taint{effect=~"NoSchedule|NoExecute"}'
    context["spare_worker"] = bool(metrics.query(query))
    return context


def deployment_snapshot(ns: str, name: str, window: str = "1m") -> dict:
    """조치 전후 비교용 스냅샷."""
    d = next((x for x in list_deployments(ns) if x["name"] == name), None)
    if d is None:
        return {"exists": False}
    pods = [p for p in list_pods(ns) if _matches(d["selector"], p["labels"]) and not p["terminating"]]
    return {
        "exists": True,
        "uid": d["uid"],
        "desired": d["desired"],
        "ready": d["ready"],
        "updated": d["updated"],
        "available": d["available"],
        "revision": d["revision"],
        "generation": d["generation"],
        "observed_generation": d["observed_generation"],
        "problem_pods": sum(1 for p in pods if p["reason"] in PROBLEM_REASONS or not p["ready"]),
        "restarts": sum(p["restarts"] for p in pods),
        "metrics": metrics.deployment_metrics(ns, name, window=window),
    }
