"""로컬 장애 사례 생성과 실측 기록을 표시하는 Streamlit 탭."""
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st

from app.incidents_ui import publish_decision, show_timeline
from kubeops_copilot import incidents
from kubeops_copilot.config import ROOT
from kubeops_copilot.campaign import DEFAULT_MIX, campaign_plan
from kubeops_copilot.scenarios import DELAYS, NAMES, single_plan, condition_rows, single_timeline
from kubeops_copilot.triage import IMPACTS, ROUTES

RESULTS = ROOT / "eval" / "results"


def new_seed():
    st.session_state["generator_seed"] = secrets.randbelow(2**31)


def condition(case):
    if case["case"] == "transient-warning":
        return f"타임아웃 {case['timeout_ms']}ms 경고와 첫 echo 요청 HTTP 503 한 번"
    if case["case"] == "bad-deploy":
        low, high = DELAYS[case["delay_profile"]]
        return f"demo/api 배포 지연 {low}~{high}ms"
    if case["case"] == "high-load":
        if "conditions" in case:
            return f"시험 API 추가 부하 목표 {case['qps']} req/s"
        return f"부하 생성기 2개 × 초당 {case.get('qps', 2000)}건 목표"
    descriptions = {"normal": "평상시 요청 유지", "service-broken": "Service selector 불일치",
        "crashloop": "새 API 컨테이너에 지원하지 않는 실행 옵션 적용", "oom": "128Mi 제한에서 메모리 256MB 요청",
        "probe-fail": "readiness 경로를 /ready로 변경", "pending": f"새 Pod에 CPU {case.get('requested_cpu', 999)}코어 요청",
        "image-pull": "존재하지 않는 이미지 태그로 새 버전 배포", "dns": "잘못된 서비스 이름과 정상 IP를 같은 클라이언트에서 호출",
        "maintenance": "워커 한 대에 새 배치 중지 후 demo/api Pod 한 개 이동", "metrics-gap": "API Pod 요청 지표 수집 중단",
        "pvc": f"없는 StorageClass에 {case.get('volume_mib', 1)}Mi 볼륨 요청"}
    return descriptions[case["case"]]


def show_report(report):
    st.caption(f"{report['started']} · 시드 {report['seed']} · 모델 {report['model']}")
    st.caption(f"평가 질문 · {report['question']}")
    for row in report["cases"]:
        name = NAMES[row["case"]]
        if "secondary" in row:
            name += " + " + NAMES[row["secondary"]["case"]]
        with st.expander(f"{name} · {'통과' if row['passed'] else '미통과'}", expanded=len(report["cases"]) == 1):
            if row.get("confirmed_patterns"):
                st.write("실제 주입 확인 · " + ", ".join(NAMES[n] for n in row["confirmed_patterns"]))
            if report.get("fixtures_only") or "secondary" in row:
                st.caption("통과 기준은 각 조건의 실측과 원상 복구입니다.")
                if row.get("route"):
                    st.write("에이전트 판단 · " + ROUTES[row["route"]])
                    st.write(row.get("diagnosis") or row.get("reason", ""))
            else:
                expected = row["expected"]
                st.caption(f"{condition(row)} · 기대 영향 {IMPACTS[expected['impact']]} · 기대 처리 {ROUTES[expected['route']]}")
                st.write(f"서비스 영향 · {IMPACTS.get(row.get('impact'), '미측정')}　"
                         f"에이전트 판단 · {ROUTES.get(row.get('route'), '미측정')}")
                st.caption(row.get("reason", ""))
                measures = [("서비스 영향", "impact", IMPACTS), ("처리 방식", "route", ROUTES),
                            ("제안 조치", "action", {"none": "변경 없음", "manual": "수동 조치", "rollback": "롤백"}),
                            ("원인 분류", "category", {"none": "이상 없음", "other": "기타", "bad_deploy": "배포 결함",
                                                  "service_misconfig": "Service 설정 오류"})]
                comparison = []
                for label, key, labels in measures:
                    actual = row.get(key)
                    comparison.append({"항목": label, "기대값": labels.get(expected[key], expected[key]),
                                       "실제 판정": labels.get(actual, actual or "미측정"),
                                       "일치": actual == expected[key] if actual is not None else None})
                st.dataframe(comparison, hide_index=True)
            if row.get("error"):
                st.error(row["error"])
            if row.get("cleanup_error"):
                st.error("원상 복구 확인 실패 · " + row["cleanup_error"])
            if row.get("approval_requested"):
                st.write("시험 승인 후 실행" if row.get("approved") else "승인 요청 후 거절")
            data = []
            for key, label in [("baseline", "주입 전"), ("injected", "주입 후"),
                               ("after_action", "에이전트 조치 후"), ("after_fixture_cleanup", "실습 원상 복구")]:
                sample = row.get(key)
                if sample:
                    data.append({"시점": label, "Ready": f"{sample['ready']}/{sample['desired']}",
                                 "엔드포인트": sample["endpoints"],
                                 "API 직접 요청": "연결 실패" if sample.get("api_code") == -1 else str(sample.get("api_code") or "미측정"),
                                 "API 응답(ms)": sample.get("api_ms"), "웹 HTTP": sample["http_status"],
                                 "응답 내용 일치": sample["echo_ok"], "웹 응답(ms)": sample["echo_ms"],
                                 "p95(ms)": sample["p95_ms"]})
            if data:
                st.dataframe(data, hide_index=True)
            sample = row.get("injected", {})
            evidence = {k: sample[k] for k in ("pod_events", "container_reasons", "dns_error", "ip_probe",
                "claim_phase", "cordoned_worker", "replacement_nodes", "warning_seen", "loadgen_ready") if sample.get(k)}
            if evidence:
                st.json(evidence, expanded=False)
            if row.get("observations"):
                st.dataframe(row["observations"], hide_index=True)
            if row.get("agent_case_id"):
                st.caption(f"조치 이력 #{row['agent_case_id']}")


def run_local(seed, case, diagnose, manual, mix=None):
    work = tempfile.TemporaryDirectory(prefix="experiment-", dir=ROOT / ".kube")
    directory = Path(work.name)
    args = [sys.executable, str(ROOT / "eval" / "single_detection.py"), "--seed", str(seed), "--case", case]
    if mix is None:
        args.extend(["--approval-dir", str(directory)])
    else:
        args.extend(["--mix", *(str(mix[t]) for t in DEFAULT_MIX), "--stop-file", str(directory / "stop")])
    if not diagnose:
        args.append("--fixtures-only")
    if manual:
        args.append("--wait-operator")
    with (directory / "progress").open("w") as log:
        process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
    st.session_state.lab_run = (work, process)


@st.fragment(run_every="2s")
def live_experiment():
    work, process = st.session_state.lab_run
    directory = Path(work.name)
    lines = (directory / "progress").read_text(errors="replace").splitlines()
    progress = [line for line in lines if line.startswith(("실행 ", "복원 대기", "단계 "))]
    if process.poll() is not None:
        reports = [line[7:] for line in lines if line.startswith("REPORT ")]
        message = ("실험 완료" if process.returncode == 0 else "실험 미완료") + (" · " + reports[-1] if reports else "")
        if any("BlockingIOError" in line for line in lines):
            message = "이미 다른 실험이 같은 클러스터를 사용 중입니다."
        st.session_state.lab_result = message
        del st.session_state.lab_run
        work.cleanup()
        st.rerun()
    st.info(progress[-1] if progress else "실험을 준비하고 있습니다.")
    if any("/100회" in line for line in progress):
        st.caption(next(line for line in reversed(progress) if "/100회" in line))
        if st.button("현재 실험 정리 후 중지", disabled=(directory / "stop").exists()):
            (directory / "stop").touch()
    identifiers = [line.removeprefix("사건 ") for line in lines if line.startswith("사건 ")]
    if identifiers:
        show_timeline(incidents.timeline(identifiers[-1]))
    pending = directory / "pending.json"
    try:
        request = json.loads(pending.read_text())
    except FileNotFoundError:
        return
    answer = directory / (request["id"] + ".json")
    if answer.exists():
        st.caption("결정을 전달했습니다.")
        return
    p = request["proposal"]
    st.markdown(f"**승인 요청** · {p['action']} · {p['namespace']}/{p['deployment']}" +
                (f" → {p['replicas']}개" if p.get("replicas") else ""))
    st.write(request["reason"], request["impact"], request["risk"])
    st.caption(request["citation"]["quote"])
    yes, no = st.columns(2)
    accepted = yes.button("이 조치 승인", key="lab_yes_" + request["id"], type="primary")
    rejected = no.button("거절", key="lab_no_" + request["id"])
    if accepted or rejected:
        publish_decision(directory, request, accepted, st.session_state.get("approver") or "운영자")
        st.rerun(scope="fragment")


def render():
    st.subheader("로그 제너레이터")
    c1, c2, c3 = st.columns([3, 2, 1])
    selected = c1.selectbox("생성할 상황", ["random", "batch", "campaign", *NAMES], format_func=lambda n: NAMES.get(n) or {"campaign": "등급 비율별 100회 평가", "batch": "단일 14종 평가 묶음", "random": "무작위 단일 상황"}[n])
    seed = c2.number_input("재현용 시드", min_value=0, max_value=2**31-1, value=42, key="generator_seed")
    c3.button("새 시드", on_click=new_seed)
    mix = None
    if selected == "campaign":
        cols = st.columns(4)
        mix = {t: col.number_input(f"{IMPACTS[t]} (%)", min_value=0, max_value=100, value=v, key="mix_" + t)
               for col, (t, v) in zip(cols, DEFAULT_MIX.items())}
        if sum(mix.values()) != 100:
            st.warning("목표 비율의 합계를 100으로 맞춰 주세요.")
    if st.button("상황 생성", type="primary", disabled=mix is not None and sum(mix.values()) != 100):
        cases = single_plan(seed)
        if selected == "campaign":
            case = {"case": selected, "mix": mix}
        elif selected == "batch":
            case = {"case": "batch"}
        elif selected == "random":
            case = cases[0]
        else:
            case = next(c for c in cases if c["case"] == selected)
        st.session_state["generated_single_scenario"] = (seed, case)
    if generated := st.session_state.get("generated_single_scenario"):
        saved_seed, case = generated
        name = case["case"]
        local = os.getenv("KUBEOPS_LOCAL_LAB") == "1" and st.get_option("server.address") in {"127.0.0.1", "localhost", "::1"}
        batch = name == "batch"
        if name == "campaign":
            planned = campaign_plan(saved_seed, case["mix"])
            st.markdown("#### 등급 비율별 100회 평가")
            st.caption(f"계획 시드 {saved_seed} · 서로 다른 사건 시드 100개 · 실측 등급은 실행 후 결정됩니다.")
            with st.expander("100회 실행 계획"):
                st.dataframe([{"회차": c["trial"], "시드": c["trial_seed"], "목표 등급": IMPACTS[c["target_tier"]],
                               "상황": NAMES[c["case"]]} for c in planned], hide_index=True)
        elif batch:
            st.markdown("#### 단일 14종 평가 묶음")
            st.caption(f"시드 {saved_seed} · 각 유형을 한 번씩 순서대로 실행합니다. 진단을 켜면 사건당 최초 판정과 필요한 경우 재판정 1회를 요청합니다.")
            st.dataframe([{"순서": i, "상황": NAMES[item["case"]], **condition_rows(item)}
                          for i, item in enumerate(single_plan(saved_seed), 1)], hide_index=True)
        else:
            # 이전 세션에 저장된 단일 미리보기도 현재 시드의 계획으로 표시한다.
            case = next(c for c in single_plan(saved_seed) if c["case"] == name)
            st.markdown("#### " + NAMES[name])
            st.caption(f"실행 계획 · 시드 {saved_seed} · " + condition(case))
            st.table([{"조건": k, "생성값": v} for k, v in condition_rows(case).items()])
            with st.expander("합성 로그 미리보기"):
                st.caption("정상 준비 이후의 계획입니다. Pod 준비와 증상 확인에 걸리는 시간은 실측 기록에 남습니다.")
                st.code("\n".join(single_timeline(case)), language="text")
        if local:
            diagnose = st.checkbox("에이전트 진단도 실행", value=True)
            manual = name != "campaign" and diagnose and st.checkbox("사람에게 인계된 Service 연결 문제는 직접 복원 대기 (최대 120초)")
            button = "단일 14종 평가 실행" if batch else "이 상황을 로컬 클러스터에서 재현"
            if name == "campaign":
                button = "100회 로컬 평가 실행"
                st.caption("장시간 순차 실행하며 진단을 켜면 모델 호출 비용이 발생합니다. 무인 평가에서는 승인 변경을 실행하지 않습니다. 매 회차 결과를 저장합니다.")
            if st.button(button,
                         disabled="lab_run" in st.session_state):
                run_local(saved_seed, name, diagnose, manual, case.get("mix"))
                st.session_state.pop("lab_result", None)
            st.caption("진단을 켜면 주입 중의 관측과 런북으로 담당자를 결정합니다. 승인 요청은 제안 내용을 확인한 뒤 응답하세요.")
    if "lab_run" in st.session_state:
        live_experiment()
    elif result := st.session_state.get("lab_result"):
        st.write(result)

    st.markdown("#### 이전 멘토링 실측 기록")
    st.button("실측 기록 새로고침")
    paths = sorted(RESULTS.glob("mentoring-*.json"), reverse=True)
    if paths:
        names = [p.name for p in paths]
        if st.session_state.get("mentoring_report") not in names:
            st.session_state["mentoring_report"] = names[0]
        pick = st.selectbox("실행 기록", names, key="mentoring_report")
        show_report(json.loads((RESULTS / pick).read_text()))
    else:
        st.caption("아직 저장된 실측 기록이 없습니다.")
