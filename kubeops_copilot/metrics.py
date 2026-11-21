"""Prometheus 메트릭 조회. 요청량·오류율·응답 시간·CPU·메모리를 Deployment 단위로 본다."""
from __future__ import annotations

import math

import requests

from .config import settings


def query(promql: str) -> list[dict]:
    try:
        r = requests.get(f"{settings.prometheus_url}/api/v1/query", params={"query": promql}, timeout=8)
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    if data.get("status") != "success":
        return []
    return data["data"]["result"]


def scalar(promql: str) -> float | None:
    res = query(promql)
    if not res:
        return None
    v = float(res[0]["value"][1])
    return None if math.isnan(v) or math.isinf(v) else v


def is_up() -> bool:
    return scalar("vector(1)") == 1.0


def _cores(value: str) -> float | None:
    if not value:
        return None
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def request_metrics(ns: str, app: str, window: str, at: int | None = None) -> dict:
    """요청 지표 세 가지의 평가 종료 시각을 같은 초에 고정할 수 있다."""
    sel = f'namespace="{ns}",app="{app}",path!~"healthz|readyz|metrics"'
    interval = f"[{window}]" + (f" @ {at}" if at is not None else "")
    req = "http_request_duration_seconds_count"
    rps = scalar(f"sum(rate({req}{{{sel}}}{interval}))")
    err = scalar(f'(sum(rate({req}{{{sel},status=~"5.."}}{interval})) or vector(0)) '
                 f"/ sum(rate({req}{{{sel}}}{interval}))")
    p95 = scalar(f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}{interval})))")
    return {"rps": rps, "error_ratio": err, "p95_ms": p95 * 1000 if p95 is not None else None}


def deployment_metrics(ns: str, app: str, window: str = "2m") -> dict:
    # 헬스체크·메트릭 수집 요청은 사용자 트래픽이 아니므로 뺀다.
    pod = f'namespace="{ns}",pod=~"{app}-[a-z0-9]+-[a-z0-9]+",container!="",container!="POD"'
    traffic = request_metrics(ns, app, window)
    cpu = scalar(f"sum(rate(container_cpu_usage_seconds_total{{{pod}}}[{window}]))")
    cpu_limit = scalar(f'sum(kube_pod_container_resource_limits{{namespace="{ns}",pod=~"{app}-[a-z0-9]+-[a-z0-9]+",resource="cpu"}})')
    throttle = scalar(f"sum(rate(container_cpu_cfs_throttled_periods_total{{{pod}}}[{window}])) "
                      f"/ sum(rate(container_cpu_cfs_periods_total{{{pod}}}[{window}]))")
    mem = scalar(f"max(container_memory_working_set_bytes{{{pod}}})")
    mem_limit = scalar(f'max(kube_pod_container_resource_limits{{namespace="{ns}",pod=~"{app}-[a-z0-9]+-[a-z0-9]+",resource="memory"}})')
    return {
        "window": window,
        "available": any(v is not None for v in (traffic["rps"], cpu, mem)),
        **traffic,
        "cpu_cores": cpu,
        "cpu_util": (cpu / cpu_limit) if cpu is not None and cpu_limit else None,
        "throttle_ratio": throttle,
        "mem_mb": mem / 1e6 if mem is not None else None,
        "mem_util": (mem / mem_limit) if mem is not None and mem_limit else None,
    }


def _f(v: float | None, fmt: str, scale: float = 1.0, suffix: str = "") -> str:
    return "-" if v is None else f"{v * scale:{fmt}}{suffix}"


def describe(m: dict) -> str:
    return (
        f"요청 {_f(m.get('rps'), '.1f', suffix=' req/s')}, 5xx 오류율 {_f(m.get('error_ratio'), '.1f', 100, '%')}, "
        f"p95 응답 {_f(m.get('p95_ms'), '.0f', suffix='ms')}\n"
        f"CPU 사용 {_f(m.get('cpu_cores'), '.3f', suffix=' core')} (limit 대비 {_f(m.get('cpu_util'), '.0f', 100, '%')}), "
        f"CPU 스로틀링 {_f(m.get('throttle_ratio'), '.0f', 100, '%')}, "
        f"메모리 최대 {_f(m.get('mem_mb'), '.0f', suffix='MB')} (limit 대비 {_f(m.get('mem_util'), '.0f', 100, '%')})"
    )
