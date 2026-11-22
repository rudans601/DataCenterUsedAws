"""평가기 저장 기록 또는 별도로 회수한 HTTP JSONL과 운영 사건을 재생한다."""
import json

import streamlit as st

from kubeops_copilot.control_replay import MODE, grade
from kubeops_copilot.triage import IMPACTS, ROUTES
from kubeops_copilot.detection import LABELS, ground_truth
from kubeops_copilot.observations import control_score, replay


def show_window(window, samples):
    if window.get("mode") == MODE:
        ref = grade(window, samples)
        st.write(f"EKS HTTP 실측 + 시드 기반 Kubernetes 기록 · {IMPACTS[ref['impact']]} · {ROUTES[ref['route']]}")
        st.caption("최초 이상 · " + (ref["initial_finding"] or "관측 없음") + " 마지막 판단 · " + ref["reason"])
        if window.get("eks_endpoints"):
            st.caption(f"EKS 실측 · Ready 엔드포인트 {window['eks_endpoints']['ready']}개")
    verdict = LABELS[ground_truth(samples)]
    st.write(f"관측 구간의 앱 HTTP · {verdict} · 요청 {sum(p['count'] for p in samples)}건 "
             f"· 실패 {sum(p['failed'] for p in samples)}건")
    stages = control_score(window)
    names = {"ok": "성공", "timeout": "시간 초과", "error": "오류"}
    for stage, title in (("read", "API 조회"), ("write", "API 변경 응답")):
        counts = stages[stage]
        st.write(title + " · " + (", ".join(f"{names[k]} {v}건" for k, v in counts.items()) or "관측 없음"))
    new = stages["new_pods"]
    st.write("새 Pod 배치 · " + ("확인 불가" if new is None else
             f"마지막 조회에서 새 Pod {new['created']}개 중 {new['assigned']}개에 노드 할당"))
    controller = [e for e in window["control"] if e["stage"] == "controller"]
    if controller:
        last = controller[-1]
        st.write(f"Deployment 목표 {last['desired']}개 · 저장 세대 {last['generation']} · "
                 f"컨트롤러가 확인한 세대 {last['observed_generation']}")
    st.caption(f"{window['start']} ~ {window['end']} · HTTP 점수는 수집한 요청에만 적용합니다. "
               "시간 초과된 변경은 저장 여부가 미확정입니다.")
    st.json({"HTTP": samples, "Kubernetes": window["control"]}, expanded=False)


def replay_upload():
    with st.expander("독립 관측 파일 재생"):
        st.caption("사건 JSON과 관측 Pod의 원본 HTTP JSONL을 선택하세요. 동일 클러스터·대상·시간 구간만 집계합니다.")
        incident = st.file_uploader("운영 사건 JSON", type="json", key="replay-control")
        http = st.file_uploader("HTTP 관측 JSONL", type=["jsonl", "log"], key="replay-http")
        if incident is None or http is None:
            return
        try:
            if max(incident.size, http.size) > 8_000_000:
                raise ValueError("파일당 8MB 이하로 관측 구간을 잘라 주세요.")
            window = json.loads(incident.getvalue())
            result = replay(window, http.getvalue().decode().splitlines())
            samples = result["samples"]
            st.write(f"사건 {window['incident_id']}")
            show_window(window, samples)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            st.error("관측 파일의 형식, 시각 또는 크기를 확인하세요.")


def show_saved(row):
    for i, window in enumerate(row.get("observation_windows", [])):
        st.markdown(f"**{window['phase']} · 독립 관측**")
        show_window(window, window.get("http_samples", []))
        raw = window.get("http_samples", [])
        if raw and all("latencies_ms" in item for item in raw):
            st.download_button("HTTP 관측 JSONL 저장", "\n".join(json.dumps(item) for item in raw),
                               file_name=f"http-{row['incident_id']}-{i}.jsonl",
                               key=f"http-jsonl-{row['incident_id']}-{i}")
        manifest = {k: v for k, v in window.items() if k not in {"http_samples", "http_score"}}
        st.download_button("운영 사건 JSON 저장", json.dumps(manifest, ensure_ascii=False),
                           file_name=f"incident-{row['incident_id']}-{i}.json",
                           key=f"incident-json-{row['incident_id']}-{i}")
    if row.get("observer_read_error"):
        st.caption("HTTP 로그 회수 실패 · 복구 후 같은 구간의 JSONL을 불러와 재생할 수 있습니다.")
