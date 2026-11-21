"""앱 요청, 단계별 사건 기록, 운영자 티켓함."""
import json
import os
from tempfile import NamedTemporaryFile
from uuid import uuid4

import streamlit as st

from kubeops_copilot import first_response, graph, incidents
from kubeops_copilot.config import ROOT

OPEN = {"접수 대기", "처리 중"}


def show_timeline(events):
    if events:
        st.dataframe([{"시각 (UTC)": e["at"], "처리 주체": e["actor"], "단계": e["action"],
                       "처리 내용": e["detail"]} for e in events], hide_index=True, width="stretch")


def publish_decision(directory, request, accepted, actor):
    """동일한 요청에서 가장 먼저 도착한 결정 하나만 워커에 전달한다."""
    with NamedTemporaryFile(mode="w", dir=directory, encoding="utf-8", delete=False) as tmp:
        json.dump({"approved": accepted, "approver": actor}, tmp, ensure_ascii=False)
    try:
        try:
            if json.loads((directory / "pending.json").read_text())["id"] != request["id"]:
                return False
            os.link(tmp.name, directory / (request["id"] + ".json"))
            return True
        except (FileExistsError, FileNotFoundError):
            return False
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass


def experiment_request(ticket_id):
    for path in (ROOT / ".kube").glob("experiment-*/pending.json"):
        try:
            request = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        if request.get("ticket_id") == ticket_id:
            return path.parent, request
    return None


def handle_demo_request(incident, message, url=first_response.ECHO_URL):
    receipt = first_response.echo(message, url)
    incidents.record_echo(incident, 1, receipt)
    if receipt["outcome"] != "escalated":
        return None
    question = "demo/web에서 demo/api echo 요청의 응답을 확인하지 못했습니다. 운영 근거와 런북을 확인하고 처리해 주세요."
    incidents.record(incident, "agent-intake", "시스템", "에이전트 진단 요청", "앱 요청의 상태와 소요 시간을 전달했습니다.")
    try:
        result = graph.ask(question, namespace="demo", thread_id=incident, observations={"echo 요청 결과": receipt})
        return {"question": question, "result": result}
    except Exception as exc:
        incidents.issue(incident, "진단 실패", "앱 복구 후속 진단을 완료하지 못했습니다.",
                        {"대상": "demo/api", "오류": type(exc).__name__, "앱 요청": receipt})
        return None


def demo_app():
    st.subheader("데모 앱 요청")
    st.caption("web을 통해 api로 메시지를 보내고 같은 내용이 돌아오는지 확인합니다. 일시적인 연결 오류는 한 번 재시도합니다.")
    with st.form("echo-demo"):
        message = st.text_input("보낼 메시지", value="kubeops-check", max_chars=256)
        send = st.form_submit_button("데모 요청 보내기")
    if send:
        incident = uuid4().hex
        st.session_state.last_echo = incident
        with st.spinner("앱 요청을 확인하고 필요한 경우 에이전트에 전달합니다."):
            turn = handle_demo_request(incident, message)
        if turn:
            st.session_state.turns.append(turn)
            st.info("에이전트의 답변과 승인 요청은 Copilot 탭에서 볼 수 있습니다.")
    if incident := st.session_state.get("last_echo"):
        show_timeline(incidents.timeline(incident))


@st.fragment(run_every="5s")
def inbox_status(shown):
    current = incidents.tickets()
    opened = sum(t["status"] in OPEN for t in current)
    st.caption(f"열린 티켓 {opened}건 · 맥 알림 요청과 운영자의 실제 접수 시각은 따로 기록합니다.")
    if [(t["id"], t["status"]) for t in current] != shown:
        st.info("새 티켓 또는 처리 상태 변경이 있습니다.")
    if st.button("티켓 목록 새로고침"):
        st.rerun()


def inbox():
    st.subheader("티켓·알림")
    tickets = incidents.tickets()
    inbox_status([(t["id"], t["status"]) for t in tickets])
    mode = st.radio("티켓 상태", ["열린 티켓", "전체"], horizontal=True)
    actor = (st.session_state.get("approver") or "").strip()
    visible = [t for t in tickets if t["status"] in OPEN or mode == "전체"]
    if not visible:
        st.info("표시할 티켓이 없습니다.")
    for t in visible:
        number = t["id"]
        with st.expander(f"#{number} · {t['kind']} · {t['status']}"):
            st.write(t["reason"])
            st.caption(t["delivery"])
            st.json(t["context"], expanded=False)
            show_timeline(incidents.timeline(t["incident"]))
            if t["status"] == "접수 대기" and st.button("담당 접수", key=f"accept_{number}", disabled=not actor):
                incidents.transition(number, "처리 중", actor, "담당자가 내용을 확인했습니다.")
                st.rerun()
            if t["status"] not in OPEN:
                continue
            if t["kind"] == "승인 요청":
                if pending := experiment_request(number):
                    directory, request = pending
                    st.write(request["reason"], request["impact"], request["risk"])
                    if (directory / (request["id"] + ".json")).exists():
                        st.caption("결정이 전달됐습니다.")
                        continue
                    yes, no = st.columns(2)
                    accept = yes.button("실험 조치 승인", key=f"yes_{number}", disabled=not actor)
                    reject = no.button("실험 조치 거절", key=f"no_{number}", disabled=not actor)
                    if accept or reject:
                        publish_decision(directory, request, accept, actor)
                        st.rerun()
                elif t["context"].get("승인 위치") == "copilot":
                    if graph.view(t["incident"]).get("pending_approval"):
                        yes, no = st.columns(2)
                        accept = yes.button("제안 승인", key=f"chat_yes_{number}", disabled=not actor)
                        reject = no.button("제안 거절", key=f"chat_no_{number}", disabled=not actor)
                        if accept or reject:
                            result = graph.decide(t["incident"], accept, actor, "티켓함에서 결정")
                            for turn in st.session_state.turns:
                                if turn["result"]["thread_id"] == t["incident"]:
                                    turn["result"] = result
                            st.rerun()
                    else:
                        st.caption("활성 승인 세션을 찾을 수 없습니다. 새 진단으로 현재 상태를 확인하세요.")
                        if st.button("지난 요청 종료", key=f"expire_{number}", disabled=not actor):
                            incidents.transition(number, "기한 종료", actor, "활성 승인 세션 없음 · 새 진단 필요")
                            st.rerun()
                else:
                    st.caption("실험 워커가 이 요청에 대한 결정을 기다리는 동안 승인 버튼이 표시됩니다.")
            elif t["status"] == "처리 중":
                with st.form(f"close_{number}"):
                    note = st.text_input("직접 한 조치와 확인 결과")
                    done = st.form_submit_button("처리 완료 기록")
                if done:
                    if note.strip() and actor:
                        incidents.transition(number, "처리 완료", actor, note.strip())
                        st.rerun()
                    else:
                        st.warning("담당자 이름과 처리 내용을 입력하세요.")
