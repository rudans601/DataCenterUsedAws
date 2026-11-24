"""같은 클러스터·대상·시간 구간의 독립 HTTP 기록과 운영 API 관측을 재생한다."""
import json
import math
from collections import Counter
from datetime import datetime, timezone

from .detection import ground_truth

HTTP_OBSERVATION_VERSION = "shared-http-v1"


def http_evidence(samples, title="최초 판정 구간 · 대표 Service HTTP 관측 묶음"):
    """채점과 공유하는 측정값만 복사한다. 정답이나 시나리오 메타데이터는 전달하지 않는다."""
    keys = ("start", "at", "cluster", "source", "target", "codes", "latencies_ms", "count", "failed", "p95_ms")
    measured = [{k: sample[k] for k in keys if k in sample} for sample in samples]
    return {"source": "probe", "target": "demo/api", "severity": "info", "title": title,
            "detail": json.dumps(measured, ensure_ascii=False, allow_nan=False)}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("관측 시각에 시간대가 필요합니다.")
    return dt


def http_samples(lines, cluster, start, end):
    """다른 사건의 표본, 양 끝 경계를 넘는 표본과 중복 로그는 집계하지 않는다."""
    left, right = instant(start), instant(end)
    if not cluster or right < left:
        raise ValueError("클러스터 식별자와 관측 구간을 확인하세요.")
    samples = {}
    for line in lines:
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("HTTP 관측 JSONL 형식을 확인하세요.")
        if (item.get("cluster"), item.get("source"), item.get("target")) != (cluster, "traffic/http-observer", "demo/api"):
            continue
        begin, finish = instant(item["start"]), instant(item["at"])
        if not left <= begin <= finish <= right:
            continue
        codes, delays = item["codes"], item["latencies_ms"]
        if not (len(codes) == len(delays) == 4 and all(type(c) is int and (c == 0 or 100 <= c <= 599) for c in codes)
                and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in delays)):
            raise ValueError("HTTP 상태 코드 또는 지연 표본이 올바르지 않습니다.")
        # 4개 표본의 nearest-rank p95는 최댓값이다.
        samples[begin, finish] = {"start": item["start"], "at": item["at"], "codes": codes,
                                  "cluster": cluster, "target": item["target"], "latencies_ms": delays,
                                  "count": len(codes), "failed": sum(not 200 <= c < 300 for c in codes),
                                  "p95_ms": max(delays), "source": item["source"]}
    return [samples[key] for key in sorted(samples)]


def replay(incident, lines):
    if incident["target"] != "demo/api" or not incident["incident_id"] or not isinstance(incident["control"], list):
        raise ValueError("demo/api 사건 형식을 확인하세요.")
    left, right = instant(incident["start"]), instant(incident["end"])
    for event in incident["control"]:
        if not left <= instant(event["at"]) <= right or event["stage"] not in {"read", "write", "controller", "placement"}:
            raise ValueError("사건 범위 또는 운영 관측 종류를 확인하세요.")
        if event["stage"] in {"read", "write"} and event["result"] not in {"ok", "timeout", "error"}:
            raise ValueError("운영 API 응답 결과를 확인하세요.")
    samples = http_samples(lines, incident["cluster"], incident["start"], incident["end"])
    counts = Counter(c for sample in samples for c in sample["codes"])
    result = {"http": ground_truth(samples), "samples": samples, "response_codes": dict(counts),
            "first": samples[0]["start"] if samples else None, "last": samples[-1]["at"] if samples else None,
            "control": control_score(incident)}
    from .control_replay import MODE, grade
    if incident.get("mode") == MODE:
        result["reference"] = grade(incident, samples)
    return result


def control_score(incident):
    """읽기·쓰기 요청 결과와 실제로 조회된 새 Pod의 배치 상태를 별도로 표시한다."""
    steps = {}
    for stage in ("read", "write"):
        counts = Counter(e["result"] for e in incident["control"] if e["stage"] == stage)
        steps[stage] = dict(counts)
    steps["new_pods"] = None
    baseline = incident.get("baseline_pod_uids")
    if baseline is not None:
        # 기준 스냅샷 자체는 '변경 후 Pod가 없음'의 근거가 될 수 없다.
        observations = [e for e in incident["control"] if e["stage"] == "placement"]
        if len(observations) >= 2:
            new = [p for p in observations[-1]["pods"] if p["uid"] not in baseline]
            steps["new_pods"] = {"created": len(new), "assigned": sum(bool(p["node"]) for p in new)}
    return steps


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incident", type=Path)
    parser.add_argument("http_log", type=Path)
    args = parser.parse_args()
    with args.http_log.open(encoding="utf-8") as stream:
        print(json.dumps(replay(json.loads(args.incident.read_text()), stream), ensure_ascii=False, indent=2))
