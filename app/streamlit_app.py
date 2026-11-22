"""KubeOps Copilot 웹 화면.

  uv run streamlit run app/streamlit_app.py

- Copilot: 자연어 질문 → 분석 근거·권장 조치 → 승인/거절 → 실행 전후 비교
- 클러스터 현황: Deployment·Pod·Service·메트릭
- 조치 이력: 질문·조치·승인자·검증 결과
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web_target import configure

EKS = configure()

from app import eks_inventory
from app.incidents_ui import demo_app, inbox, show_timeline
from kubeops_copilot import incidents
from app.scenario_ui import RESULTS, render as render_scenarios
from app.operations_ui import render as render_operations
from app.detection_ui import render as render_detection
from kubeops_copilot import audit, graph, k8s_tools, metrics, rag  # noqa: E402
from kubeops_copilot.config import policy, settings  # noqa: E402
from kubeops_copilot.llm import is_configured
from kubeops_copilot.triage import IMPACTS, ROUTES  # noqa: E402

st.set_page_config(page_title="KubeOps Copilot", page_icon="☸️", layout="wide")

SEVERITY = {"critical": "🔴 심각", "warning": "🟠 주의", "info": "🟢 정상"}
STATUS = {
    "healthy": ("정상", "green"), "degraded": ("성능 저하", "orange"), "failing": ("장애", "red"),
}
RUN_STATUS = {
    "handoff": "사람에게 인계",
    "answered": "답변 완료", "blocked": "실행 차단", "rejected": "승인 거절",
    "executed": "조치 실행", "failed": "실행 실패",
}
CATEGORY = {
    "scheduling": "스케줄링 실패", "image_pull": "이미지 다운로드 실패", "dns": "DNS 조회 실패",
    "maintenance": "워커 점검", "metrics_gap": "지표 누락", "storage": "볼륨 할당 대기",
    "none": "이상 없음", "crashloop": "반복 재시작", "oom": "메모리 부족", "probe_failure": "Probe 실패",
    "service_misconfig": "Service 설정 오류", "high_load": "부하 증가", "bad_deploy": "배포 결함", "other": "기타",
}
EXAMPLES = [
    "지금 demo 서비스들 상태 어때? 문제 있는 거 있어?",
    "api 응답이 느려진 것 같아. 원인이 뭐고 자원 증설이 필요해?",
    "api 배포하고 나서 Pod가 계속 재시작되는 것 같아. 원인이 뭐고 어떻게 해야 해?",
    "web에서 api 호출이 전부 실패하고 있어. 뭐가 문제야?",
    "kube-system 네임스페이스의 coredns를 재시작해줘",
]

st.session_state.setdefault("turns", [])


# ---------------------------------------------------------------- 사이드바
@st.cache_data(ttl=20, show_spinner=False)
def connection_status() -> dict:
    out = {}
    try:
        out["k8s"] = (True, f"Deployment {len(k8s_tools.list_deployments(settings.namespace))}개")
    except Exception:  # noqa: BLE001
        out["k8s"] = (False, "에이전트 kubeconfig와 토큰 만료 여부를 확인하세요.")
    out["prom"] = (metrics.is_up(), settings.prometheus_url)
    out["llm"] = (is_configured(), settings.llm_model)
    return out


@st.cache_data(ttl=30, show_spinner=False)
def worker_inventory():
    return eks_inventory.inventory()


with st.sidebar:
    st.title("☸️ KubeOps Copilot")
    st.caption("데이터 센터 Kubernetes 통합 운영 지원 AI Agent")
    if EKS:
        st.subheader("AWS 서울 · EKS")
        st.code(eks_inventory.CLUSTER)
        if not os.getenv("KUBEOPS_EKS_VERIFIED_ENDPOINT"):
            st.warning("EKS 실시간 연결을 기다리고 있습니다.")
            st.caption("저장된 AWS 실측 결과는 아래에서 확인할 수 있습니다.")

if EKS and not os.getenv("KUBEOPS_EKS_VERIFIED_ENDPOINT"):
    st.header("EKS 실측 기록")
    st.info("실시간 조회는 전용 클러스터 연결 후 사용할 수 있습니다.")
    render_detection(RESULTS)
    st.stop()


with st.sidebar:
    status = connection_status()
    for key, label in [("k8s", "Kubernetes"), ("prom", "Prometheus"), ("llm", settings.llm_provider)]:
        ok, detail = status[key]
        st.text(f"{'🟢' if ok else '🔴'} {label} · {detail}")
    st.divider()
    st.text_input("승인자 이름", key="approver", value="운영자")
    st.markdown("**승인 후 실행 범위**")
    for action in sorted(policy.allowed_actions):
        st.markdown(f"- {policy.descriptions[action]}")
    st.caption(f"대상: demo/{', demo/'.join(sorted(policy.allowed_deployments))} · "
               f"복제본 {policy.min_replicas}~{policy.max_replicas}개")
    st.divider()
    with st.expander("위험 등급과 처리 기준"):
        st.write("정상은 대표 요청과 서비스 상태가 안정적인 경우, 주의는 서비스가 응답하지만 이상 신호가 있는 경우입니다. "
                 "실패율 5% 또는 p95 300ms를 초과하면 장애입니다. 연속된 요청 구간이 전부 실패하고 "
                 "연결 가능한 Pod가 없으면 중대 장애입니다. 근거가 부족하면 판단을 보류합니다.")
        st.write(f"에이전트가 조회한 근거와 런북 v{rag.RUNBOOK_VERSION}의 원문을 연결해 각 사건의 등급과 담당자를 판단합니다.")
        st.markdown("**앱 1차 복구**  \n같은 echo 요청을 한 번 재시도하고 응답 내용을 확인합니다. 실패가 계속되거나 클러스터에 다른 이상이 남으면 에이전트가 검토합니다.")
        st.markdown("**자동 관찰**  \n대표 요청이 안정적이거나 경고가 일시적이면 추가 표본을 확인합니다.")
        if not EKS:
            st.markdown("**제한 자동 조치**  \n로컬 demo/api의 지속 부하에 +1 증설, 또는 기존 버전이 정상인 실패 리비전 롤백. 전용 절차의 조건을 실행 직전 다시 확인합니다.")
        st.markdown("**운영자 승인 후 실행**  \n허용된 변경의 대상, 근거, 영향과 위험을 제시하고 승인을 기다립니다.")
        st.markdown("**사람이 직접 처리**  \n비용 발생 가능성, 권한 밖 변경, 근거 부족, 복구 미확인 시 티켓을 발급합니다. 운영자가 접수해 조치와 결과를 기록합니다.")
    st.markdown("**예시 질문**")
    for q in EXAMPLES:
        if st.button(q, use_container_width=True, key=f"ex-{q}"):
            st.session_state["queued_question"] = q
    st.divider()
    if st.button("운영 문서 다시 색인", use_container_width=True):
        with st.spinner("색인 중..."):
            store = rag.get_store(force=True)
        st.success(f"문서 {len(store.store)}개 색인 완료")


# ---------------------------------------------------------------- 결과 렌더링
def show_triage(decision):
    if decision:
        st.markdown(f"**위험 등급** · {IMPACTS[decision['impact']]}　 **처리 방식** · {ROUTES[decision['route']]}")
        if decision.get("impact_reason") and decision["impact_reason"] != decision.get("reason"):
            st.caption(decision["impact_reason"])
        if decision.get("model_route") and decision["model_route"] != decision["route"]:
            st.caption("에이전트의 원래 제안 · " + ROUTES.get(decision["model_route"], decision["model_route"]))
        st.caption(decision["reason"])


def render_result(turn: dict) -> None:
    res = turn["result"]
    a = res.get("analysis") or {}
    tid = res["thread_id"]
    if not a:
        st.error("분석 결과가 없어요.")
        return

    label, color = STATUS.get(a.get("status"), ("-", "gray"))
    g = a.get("grounding") or {}
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(f"**상태**  \n:{color}[● {label}]")
    c2.markdown(f"**원인 분류**  \n{CATEGORY.get(a.get('root_cause_category'), '-')}")
    confidence = {"high": "높음", "medium": "보통", "low": "낮음"}.get(a.get("confidence"), "-")
    c3.markdown(f"**확신도**  \n{confidence}")
    c4.markdown(f"**근거 인용**  \n{g.get('grounded', 0)}/{g.get('findings', 0)}개 판단")
    c5.markdown(f"**분석 시간**  \n{(res.get('timings') or {}).get('analysis_total', '-')}초")

    show_triage(res.get("triage"))
    if ticket := (res.get("triage") or {}).get("ticket_id"):
        st.info(f"운영자 티켓 #{ticket} · 조치 → 티켓·알림에서 확인하세요.")
    with st.expander("단계별 처리 기록"):
        show_timeline(incidents.timeline(tid))
    if a.get("citation_source"):
        st.caption(f"판단에 사용한 런북 v{a['runbook_version']} · {a['citation_source']}")
        st.write(a["runbook_citation"]["quote"])

    st.markdown(res.get("answer") or a.get("answer_markdown", ""))

    with st.expander(f"판단 근거 (근거 {len(res.get('evidence', []))}개)"):
        for f in a.get("findings", []):
            ids = " ".join(f"`{i}`" for i in f["evidence_ids"]) or ":red[근거 없음]"
            st.markdown(f"- {f['claim']} {ids}")
        ev = pd.DataFrame([
            {"ID": e["id"], "심각도": SEVERITY.get(e["severity"], e["severity"]), "출처": e["source"],
             "대상": e["target"], "제목": e["title"], "내용": e["detail"]}
            for e in res.get("evidence", [])
        ])
        st.dataframe(ev, hide_index=True, use_container_width=True,
                     column_config={"내용": st.column_config.TextColumn(width="large")})

    docs = [d for d in res.get("docs", []) if d.get("content")]
    if docs:
        with st.expander(f"참고 문서 ({len(docs)}건)"):
            cited = set(a.get("doc_ids", []))
            for d in docs:
                mark = "**인용**" if d["id"] in cited else ""
                kind = "런북" if d["kind"] == "runbooks" else "장애 사례"
                st.markdown(f"- `{d['id']}` [{kind}] {d['title']} · `{d['source']}` · {('유사도 ' + str(d['score'])) if d.get('score') is not None else '공통 절차'} {mark}")

    guard = res.get("guard") or {}
    p = (guard.get("normalized") or res.get("proposal") or {})
    if guard.get("executable") and (not guard.get("allowed") or (res.get("triage") or {}).get("route", "approval") == "approval"):
        st.markdown("#### 권장 조치")
        target = f"{p.get('namespace')}/{p.get('deployment')}"
        replicas = f" → 복제본 {p['replicas']}개" if p.get("replicas") else ""
        prop = res.get("proposal") or {}
        box = st.container(border=True)
        box.markdown(f"**{p.get('action')}** `{target}`{replicas}")
        box.markdown(f"- 이유: {prop.get('reason', '-')}\n- 예상 영향: {prop.get('expected_impact', '-')}\n"
                     f"- 위험: {prop.get('risk', '-')}")
        if not guard.get("allowed"):
            box.error("허용 범위 밖이라 실행할 수 없어요: " + " ".join(guard.get("reasons", [])))
        elif res.get("pending_approval"):
            box.warning("운영자 승인 전에는 실행되지 않아요.")
            b1, b2, _ = box.columns([2, 1, 4])
            approver = st.session_state.get("approver") or "운영자"
            if b1.button("승인하고 실행", key=f"ok-{tid}", type="primary"):
                with st.spinner(f"조치를 실행하고 결과를 검증하는 중이에요 (약 {settings.verify_seconds + 30}초)..."):
                    turn["result"] = graph.decide(tid, True, approver)
                st.rerun()
            if b2.button("거절", key=f"no-{tid}"):
                turn["result"] = graph.decide(tid, False, approver)
                st.rerun()

    ex = res.get("execution")
    if ex:
        st.markdown("#### 실행 결과")
        actor = "제한 자동 조치" if ex.get("mode") == "auto" else f"승인자 {ex['approver']}"
        (st.success if ex.get("ok") else st.error)(f"{ex['message']} · {actor} · {ex.get('at', '')}")
    v = res.get("verification")
    if v and v.get("rows"):
        verdict = {"success": "성공", "partial": "부분 개선", "failed": "실패", "unknown": "검증 불가"}.get(v["verdict"], v["verdict"])
        st.markdown(f"#### 결과 검증: {verdict}")
        st.caption(v.get("summary", ""))
        st.dataframe(pd.DataFrame(v["rows"]), hide_index=True, use_container_width=True)
    if res.get("status") in RUN_STATUS:
        st.caption(f"처리 상태: {RUN_STATUS[res['status']]} · 이력 #{res.get('case_id', '-')}")


def incident_stream(question, tid):
    try:
        yield from graph.stream(question, tid)
    except Exception as exc:
        incidents.issue(tid, "실행 검토", "에이전트 처리가 중단되어 현재 상태 확인이 필요합니다.",
                        {"오류": type(exc).__name__})
        raise


def run_question(question: str) -> None:
    tid = uuid.uuid4().hex[:12]
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        with st.status("분석을 시작했어요", expanded=True) as box:
            for node, update in incident_stream(question, tid):
                if node == "__interrupt__":
                    box.write("⏸ 운영자 승인 대기")
                    continue
                label = graph.NODE_LABELS.get(node, node)
                extra = ""
                if node == "collect":
                    extra = f" · 근거 {len(update.get('evidence', []))}개"
                elif node == "retrieve":
                    extra = f" · 문서 {len(update.get('docs', []))}건"
                box.write(f"✓ {label}{extra}")
            box.update(label="분석 완료", state="complete", expanded=False)
    st.session_state["turns"].append({"question": question, "result": graph.view(tid)})
    st.rerun()


# ---------------------------------------------------------------- 탭
tab_chat, tab_scenarios, tab_matrix, tab_cluster, tab_actions = st.tabs(
    ["Copilot", "상황 생성기", "혼동행렬", "EKS · EC2 현황" if EKS else "클러스터 현황", "조치"]
)
with tab_cluster:
    tab_overview, tab_operations, tab_history = st.tabs(["현황", "상세 모니터링 (Grafana)", "조치 이력"])
with tab_actions:
    tab_demo, tab_inbox = st.tabs(["데모 앱", "티켓·알림"])

with tab_demo:
    demo_app()

with tab_inbox:
    inbox()

with tab_scenarios:
    render_scenarios()

with tab_matrix:
    render_detection(RESULTS)

with tab_chat:
    if not st.session_state["turns"]:
        st.info("운영 상태, 응답 지연, 자원 증설 필요성, 장애 원인을 자연어로 물어보세요. "
                "에이전트가 런북을 찾아 관찰·자동 조치·승인 요청·사람 인계를 판단해요.")
    for turn in st.session_state["turns"]:
        with st.chat_message("user"):
            st.write(turn["question"])
        with st.chat_message("assistant"):
            render_result(turn)
    question = st.chat_input("예: api 응답이 느려진 것 같은데 원인이 뭐야?")
    question = question or st.session_state.pop("queued_question", None)
    if question:
        try:
            run_question(question)
        except Exception as exc:
            st.error(f"에이전트 처리를 마치지 못했습니다 ({type(exc).__name__}). 조치 → 티켓·알림에서 확인하세요.")

@st.fragment(run_every="30s" if EKS else None)
def render_overview():
    if EKS:
        checked = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S")
        st.caption(f"최근 조회 {checked} KST · 30초마다 갱신")
    if st.button("새로고침"):
        st.cache_data.clear()
        st.rerun()
    if EKS:
        st.subheader("EC2 워커")
        try:
            fleet = worker_inventory()
            st.caption(f"{eks_inventory.REGION} · EKS {fleet['version']} ({fleet['status']}) · "
                       f"{eks_inventory.NODE_GROUP} ({fleet['group_status']}) · 목표 {fleet['desired']}대")
            if fleet["workers"]:
                st.dataframe(fleet["workers"], hide_index=True)
            else:
                st.info("관리형 노드 그룹에 조회되는 EC2 인스턴스가 없습니다.")
        except (KeyError, ValueError, OSError, subprocess.SubprocessError):
            st.warning("AWS에서 워커를 조회하지 못했습니다. AWS CLI 세션 및 EKS·EC2 조회 권한을 확인하세요.")
    try:
        ns = settings.namespace
        deps = k8s_tools.list_deployments(ns)
        st.subheader(f"Deployment ({ns})")
        st.dataframe(pd.DataFrame([{
            "이름": d["name"], "Ready": f"{d['ready']}/{d['desired']}", "갱신": d["updated"], "리비전": d["revision"],
            "change-cause": d["change_cause"], "이미지": d["image"], "인자": " ".join(d["args"]),
            "limit": f"cpu {d['cpu_limit']} / mem {d['memory_limit']}", "롤아웃": d["progressing"],
        } for d in deps]), hide_index=True, use_container_width=True)

        st.subheader("메트릭 (최근 2분)")
        rows = []
        for d in deps:
            m = metrics.deployment_metrics(ns, d["name"])
            fmt = lambda v, s=1.0, f=".1f": "-" if v is None else format(v * s, f)  # noqa: E731
            rows.append({"이름": d["name"], "요청(req/s)": fmt(m["rps"]), "5xx(%)": fmt(m["error_ratio"], 100),
                         "p95(ms)": fmt(m["p95_ms"], 1, ".0f"), "CPU(limit 대비 %)": fmt(m["cpu_util"], 100, ".0f"),
                         "스로틀링(%)": fmt(m["throttle_ratio"], 100, ".0f"), "메모리(MB)": fmt(m["mem_mb"], 1, ".0f")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.subheader("Pod")
        st.dataframe(pd.DataFrame([{
            "이름": p["name"], "상태": p["reason"] or p["state"], "Ready": p["ready"], "재시작": p["restarts"],
            "마지막 종료": p["last_termination"], "리비전": p["revision"], "노드": p["node"], "생성": p["age"],
        } for p in k8s_tools.list_pods(ns)]), hide_index=True, use_container_width=True)

        st.subheader("Service")
        st.dataframe(pd.DataFrame([{
            "이름": s["name"], "타입": s["type"], "selector": str(s["selector"]), "포트": ", ".join(s["ports"]),
            "일치 Pod": s["matching_pods"], "Ready 엔드포인트": s["ready_endpoints"],
        } for s in k8s_tools.service_status(ns)]), hide_index=True, use_container_width=True)
    except Exception as e:  # noqa: BLE001
        if EKS:
            st.warning("EKS의 워크로드를 조회하지 못했습니다. AWS 로그인과 클러스터 연결 상태를 확인하세요.")
        else:
            st.error(f"클러스터 정보를 가져오지 못했어요: {e}")


with tab_overview:
    render_overview()

with tab_history:
    cases = audit.list_cases(200)
    if not cases:
        st.info("아직 이력이 없어요.")
    else:
        df = pd.DataFrame(cases).rename(columns={
            "id": "#", "created_at": "시각", "question": "질문", "status": "처리", "category": "원인 분류",
            "action": "조치", "target": "대상", "approver": "승인자", "verdict": "검증", "analysis_seconds": "분석(초)"})
        df["처리"] = df["처리"].map(lambda s: RUN_STATUS.get(s, s))
        df["원인 분류"] = df["원인 분류"].map(lambda s: CATEGORY.get(s, s))
        st.dataframe(df, hide_index=True, use_container_width=True)
        pick = st.selectbox("자세히 볼 이력", [c["id"] for c in cases], format_func=lambda i: f"#{i}")
        case = audit.get_case(pick)
        if case:
            show_triage(case["payload"].get("triage"))
            st.markdown(case["payload"].get("answer") or "")
            v = case["payload"].get("verification") or {}
            if v.get("rows"):
                st.dataframe(pd.DataFrame(v["rows"]), hide_index=True, use_container_width=True)

with tab_operations:
    render_operations(RESULTS)
