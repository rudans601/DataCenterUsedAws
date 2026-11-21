"""단일 상황의 사건 단위 서비스 장애 탐지 평가."""
from collections import Counter
import json
from datetime import datetime, timedelta
import os
from urllib.parse import urlencode

import streamlit as st

from app.incidents_ui import show_timeline
from kubeops_copilot import incidents, request_safety
from kubeops_copilot.triage import IMPACTS, ROUTES
from kubeops_copilot.detection import CELLS, LABELS, PROTOCOL, LEGACY_PROTOCOL, bucket, decision_matrix, experiment_impact, report_rows, statistics, severity_statistics, handoff_counts
from kubeops_copilot.scenarios import NAMES, RECOVERY, condition_rows


def dashboard_url():
    base = os.getenv("KUBEOPS_GRAFANA_URL") or (
        "http://127.0.0.1:3000" if os.getenv("KUBEOPS_LOCAL_LAB") == "1" else "")
    return f"{base.rstrip('/')}/d/kubeops-demo-api/demo-api" if base else None


def dashboard_period(row):
    episodes = row.get("episodes") or []
    starts = [e["injection_started"] for e in episodes if e.get("injection_started")]
    if not starts:
        return None
    samples = [e["measured_until"] for e in episodes if e.get("measured_until")]
    samples += [p["at"] for p in row.get("probes", []) if p.get("at")]
    start = datetime.fromisoformat(min(starts))
    last = datetime.fromisoformat(max([*starts, *samples]))
    recovered = row.get("recovery_verified_at")
    end = max(last, datetime.fromisoformat(recovered)) if recovered else last
    before = min(start, datetime.fromisoformat(row["baseline_at"])) if row.get("baseline_at") else start
    params = {"from": int((before - timedelta(minutes=2)).timestamp() * 1000),
              "to": int((end + timedelta(minutes=3)).timestamp() * 1000)}
    return start, last, urlencode(params)


def show_rows(rows):
    for row in rows:
        label = f"{NAMES[row['case']]} · 시드 {row['seed']} · {row['started'][:19].replace('T', ' ')}"
        with st.expander(label):
            if row.get("incident_id") and not row.get("fixtures_only"):
                events = incidents.timeline(row["incident_id"]) or row.get("timeline", [])
                tickets = incidents.tickets(row["incident_id"]) or row.get("tickets", [])
                show_timeline(events)
                if tickets:
                    st.caption(" · ".join(f"티켓 #{t['id']} {t['kind']} ({t['status']})" for t in tickets))
                st.download_button("현재 처리 이력 포함 JSON", json.dumps({**row, "timeline": events, "tickets": tickets},
                                   ensure_ascii=False, indent=2), file_name=f"{row['incident_id']}.json",
                                   mime="application/json", key="download-" + row["incident_id"])
            impact, reason = experiment_impact(row)
            st.write(f"실측 위험 등급 · **{IMPACTS[impact]}**")
            st.caption(reason)
            if row.get("target_tier"):
                st.caption(f"100회 계획 시드 {row['campaign_seed']} · {row['trial']}회차 · 목표 {IMPACTS[row['target_tier']]}")
            if check := row.get("recheck"):
                st.write("재측정 결과", {"held": "보류 지속", "error": "측정·재판정 오류", "resolved": "판단 확정"}[check["status"]])
                st.json(check, expanded=False)
            if row.get("model_route"):
                ref = row.get("reference") or {}
                st.write(f"에이전트 등급 · {IMPACTS[row['model_impact']]}　 모델 제안 · {ROUTES[row['model_route']]}　 "
                         f"정책 적용 후 · {ROUTES[row['effective_route']]}")
                st.caption(row["decision"]["reason"])
                st.caption(f"채점 기준 · {ROUTES.get(ref.get('route'), '미확정')} · {ref.get('reason', '')}")
                if row.get("citation"):
                    st.write(row["citation"]["quote"])
                    st.caption(f"런북 v{row.get('runbook_version')} · " + (row.get("citation_source") or "원문 인용 확인 실패"))
                st.write("에이전트 조치 결과 · " + row.get("recovery_outcome", "미실행"))
                if row.get("effective_route") == "handoff" or row.get("recovery_outcome") in {"복구 실패", "부분 개선", "검증 불가", "실행 실패"}:
                    st.write(row.get("root_cause") or row.get("diagnosis", ""))
                    for step in row.get("manual_steps", []):
                        st.markdown("- " + step)
                if row.get("approval"):
                    st.caption(f"승인 기록 · {row['approval']['approver']} · {row['approval'].get('comment', '')}")
            st.write(f"실측 정답 **{LABELS.get(row.get('truth'), '미측정')}**　 "
                     f"에이전트 **{LABELS.get(row.get('prediction'), '미판정')}**　 · {bucket(row)}")
            base_url = dashboard_url()
            period = dashboard_period(row) if base_url else None
            if period:
                start, last, query = period
                st.link_button("Grafana에서 장애 전후 보기", f"{base_url}?{query}")
                st.caption(f"주입 시작 {start:%H:%M:%S} · 마지막 관측 {last:%H:%M:%S}"
                           + (f" · 복구 확인 {datetime.fromisoformat(row['recovery_verified_at']):%H:%M:%S}"
                              if row.get("recovery_verified_at") else ""))
            if row.get("diagnosis"):
                st.write(row["diagnosis"])
            st.table([{"조건": k, "생성값": v} for k, v in condition_rows(row).items()])
            if row.get("probes"):
                probes = [{"측정 시각": p["at"], "요청": p["count"], "실패": p["failed"], "p95 (ms)": p["p95_ms"]}
                          for p in row["probes"]]
                st.dataframe(probes, hide_index=True)
            details = {k: row[k] for k in ("remaining_capacity", "episodes", "pulses", "injected", "evidence", "action_result",
                                             "documents", "findings", "before_action", "pre_action_probes", "verification", "after_action_probes", "after_action", "after_fixture_cleanup") if row.get(k)}
            if details:
                st.json(details, expanded=False)
            if action := row.get("proposed_action"):
                labels = {"none": "관찰", "manual": "운영자 확인", "rollback": "롤백",
                          "restart": "재시작", "scale": "복제본 조정"}
                st.write("에이전트가 제안한 조치 · " + labels.get(action, action))
            if row.get("recovered_by"):
                st.caption("실제 회복 처리 · " + RECOVERY.get(row["recovered_by"], row["recovered_by"]))
            if after := row.get("after_fixture_cleanup"):
                st.caption(f"정리 후 확인 · API HTTP {after.get('api_code')} · Ready {after.get('ready')}/{after.get('desired')}")
            for k in ("error", "prediction_error", "cleanup_error"):
                if row.get(k):
                    st.warning(row[k])
            st.caption(row["model"] + " · " + row["report"])


ACTION_NAMES = {"none": "관찰", "manual": "사람 처리", "rollback": "롤백", "restart": "재시작", "scale": "복제본 조정"}


def show_decisions(rows):
    for field, title, labels in [("impact", "위험 등급", IMPACTS), ("route", "처리 방식", ROUTES), ("action", "조치 선택", ACTION_NAMES)]:
        pairs, missing = decision_matrix(rows, field)
        st.markdown(f"#### {title} 혼동행렬")
        st.caption(f"행은 독립 채점 기준, 열은 재측정이 있으면 재판정까지 반영한 에이전트의 제안입니다. 기준 미확정 {missing}건.")
        if pairs:
            columns = list(dict.fromkeys([*(a for a, _ in pairs), *(b for _, b in pairs)]))
            st.dataframe([{"실제 기준": labels.get(a, a), **{labels.get(b,b): pairs[a,b] for b in columns}}
                          for a in dict.fromkeys(a for a, _ in pairs)], hide_index=True)
        else:
            st.caption("선택한 기록에 채점 가능한 판단이 없습니다.")
    judged = [r for r in rows if r.get("prediction_status") == "complete"]
    if judged:
        st.caption(f"현행 런북 원문 인용 확인 {sum(bool(r.get('citation_source')) for r in judged)}/{len(judged)}건")
    st.markdown("#### 실제 복구 결과")
    records = [r for r in rows if not r.get("fixtures_only")]
    checked = [r for r in records if r.get("app_checks")]
    if checked:
        st.write(f"앱 1차 처리 {len(checked)}건 · 앱 복구로 종결 {sum(r.get('prediction_status') == 'app_recovered' for r in checked)}건 · "
                 f"에이전트 진단 요청 {sum(any(e['action']=='에이전트 진단 요청' for e in r.get('timeline', [])) for r in checked)}건")
    counts = Counter(("앱 1차 처리" if r.get("prediction_status") == "app_recovered" else r.get("effective_route", "미판정"),
                      r.get("recovery_outcome", "미실행")) for r in records)
    if counts:
        outcomes = list(dict.fromkeys(outcome for _, outcome in counts))
        st.dataframe([{"실행 경로": ROUTES.get(route, route),
                       **{outcome: counts[route, outcome] for outcome in outcomes}}
                      for route in dict.fromkeys(route for route, _ in counts)], hide_index=True)
        attempts = [r for r in records if r.get("action_result")]
        successes = sum(r.get("recovery_outcome") in ("서비스 복구", "배포 복구") for r in attempts)
        st.caption(f"에이전트 실행 시도 {len(attempts)}건 중 복구 확인 {successes}건. 운영자 직접 처리와 실험 종료 후 복원은 복구 실적에서 제외합니다.")
    st.caption("에이전트 변경 직후, 실험 정리 전에 확인한 결과입니다. 서비스 복구와 실패한 배포의 복구를 각각 기록합니다.")


def fraction(n, d):
    return f"{n}/{d} ({n/d:.1%})" if d else "0/0 · 계산 불가"


def show_tier_metrics(rows):
    st.markdown("#### 실측 위험 등급별 서비스 탐지")
    targets = Counter(r["target_tier"] for r in rows if r.get("target_tier") and not r.get("fixtures_only"))
    table = []
    for tier, (size, counts, _, handoff) in severity_statistics(rows).items():
        table.append({"등급": IMPACTS[tier], **({"실행된 목표 건수": targets[tier]} if targets else {}), "실측 건수": size, **{k: counts[k] for k in CELLS},
                      "미탐률": fraction(counts["FN"], counts["TP"] + counts["FN"]),
                      "오탐률": fraction(counts["FP"], counts["TN"] + counts["FP"]),
                      "최종 보류": handoff["held"], "사람 티켓": handoff["ticketed"]})
    st.dataframe(table, hide_index=True)
    st.caption("각 등급 안에서 독립 HTTP 실측과 최종 이진 판정을 비교합니다. 보류·미완료·앱 자체 복구는 두 비율의 분모에서 제외합니다.")
    h = handoff_counts(rows)
    st.metric("보류 건의 티켓 인계율", fraction(h["ticketed"], h["held"] - h["untracked"]))
    st.caption(f"재측정으로 판단 확정 {h['resolved']}건 · 최종 보류 {h['held']}건 · 티켓 이력 미수집 {h['untracked']}건. "
               "동일 사건의 사람 인계 티켓 발급을 확인합니다. 승인 요청은 제외하며 이력 미수집 건은 별도 표시합니다.")


def show_request_safety(directory):
    st.markdown("### 조치 요청 차단 평가")
    runs = dict(request_safety.reports(directory))
    if not runs:
        st.caption("아직 조치 요청 차단 평가 기록이 없습니다.")
        return
    def run_label(path):
        report = runs[path]
        mode = "실행 가드 검사 · 고정 제안" if report.get("mode") == "policy" else f"에이전트 평가 · {report['model']}"
        return report["started"][:19].replace("T", " ") + " · " + mode

    path = st.selectbox("차단 평가 실행 기록", list(runs), key="safety_report", format_func=run_label)
    report = runs[path]
    rows = report["rows"]
    score = request_safety.summarize(rows)
    (st.success if score["passed"] else st.error)("가드레일 통과" if score["passed"] else "가드레일 미통과")
    first, second, third = st.columns(3)
    first.metric("차단 재현율", fraction(score["blocked"], score["dangerous"]))
    second.metric("위험 요청 통과", f"{score['leaks']}건")
    third.metric("오차단율", fraction(score["false_blocks"], score["legitimate"]))
    st.caption(f"기준은 위험 요청 통과 0건과 오차단율 {request_safety.MAX_FALSE_BLOCK_RATE:.0%} 이하입니다. "
               f"판정 오류·미확정 {score['unresolved']}건. 정상 승인 대기는 허용으로 집계합니다.")
    st.dataframe([
        {"기대 결과": "위험 요청", "차단": score["blocked"], "통과": score["leaks"]},
        {"기대 결과": "정당한 요청", "차단": score["false_blocks"], "통과": score["allowed"]},
    ], hide_index=True)
    with st.expander("차단 평가 사례와 판정 근거"):
        labels = {"block": "차단", "allow": "통과", "unknown": "미확정"}
        st.caption(f"프롬프트 {report['prompt_version']} · 문서 {report['corpus_revision']}")
        st.dataframe([{"사례": r["label"], "요청": r["question"], "기대": labels[r["expected"]],
                       "결과": labels[r["actual"]], "사유": r["reason"]} for r in rows], hide_index=True)


def render(directory):
    st.button("평가 기록 새로고침", key="cm_refresh")
    show_request_safety(directory)
    st.subheader("서비스 장애 탐지 혼동행렬")
    st.caption("로컬 demo/api의 단일 실험 1회가 1건입니다. 행은 독립 HTTP 실측 정답, 열은 에이전트 판정입니다.")
    all_rows = report_rows(directory)
    protocol = st.radio("평가 버전", [PROTOCOL, LEGACY_PROTOCOL], horizontal=True,
                        format_func=lambda v: "런북 v2 · 탐지·조치·복구" if v == PROTOCOL else "기존 탐지 기록")
    rows = [r for r in all_rows if r["protocol"] == protocol]
    if not rows:
        st.info("아직 단일 탐지 평가 기록이 없습니다. 상황 생성기에서 에이전트 진단을 켜고 단일 실습을 실행하세요.")
        return
    if protocol == PROTOCOL:
        versions = list(dict.fromkeys((r.get("prompt_version") or "초기", r.get("corpus_revision") or "초기") for r in rows))
        version = st.selectbox("프롬프트·문서 버전", versions, format_func=lambda v: " · ".join(v))
        rows = [r for r in rows if (r.get("prompt_version") or "초기", r.get("corpus_revision") or "초기") == version]
    a, b, c, run_filter = st.columns(4)
    case = a.selectbox("상황 유형", ["전체", *NAMES], format_func=lambda v: NAMES.get(v, v), key="cm_case")
    seed = b.selectbox("평가 시드", ["전체", *sorted({r["seed"] for r in rows})], key="cm_seed")
    model = c.selectbox("판정 모델", ["전체", *sorted({r["model"] for r in rows})], key="cm_model")
    report = run_filter.selectbox("실행 기록", ["전체", *dict.fromkeys(r["report"] for r in rows)],
                                  format_func=lambda s: s.removeprefix("detection-").removesuffix(".json"), key="cm_run")
    selected = [r for r in rows if (report == "전체" or r["report"] == report) and (case == "전체" or r["case"] == case) and
                (seed == "전체" or r["seed"] == seed) and (model == "전체" or r["model"] == model)]
    counts, metrics = statistics(selected)
    cols = st.columns(3)
    for i, (name, rate) in enumerate(metrics.items()):
        cols[i % 3].metric(name, f"{rate:.1%}" if rate is not None else "계산 불가")
    header = st.columns([1, 2, 2])
    header[1].markdown("**장애로 판정**")
    header[2].markdown("**정상으로 판정**")
    cell_names = {"TP": "장애 탐지", "FN": "미탐", "FP": "오탐", "TN": "정상 판정"}
    for actual, cells in [("실제 장애", ("TP", "FN")), ("실제 정상", ("FP", "TN"))]:
        left, *buttons = st.columns([1, 2, 2])
        left.markdown("**" + actual + "**")
        for button, cell in zip(buttons, cells):
            if button.button(f"{cell} {cell_names[cell]} · {counts[cell]}건", key="matrix_" + cell, use_container_width=True):
                st.session_state.cm_detail = cell
    other = {key: value for key, value in counts.items() if key not in CELLS}
    st.caption(f"상황 {len({r['case'] for r in selected})}종 · 실행 " + str(len(selected)) + "건 · 이진 판정 " + str(sum(counts[k] for k in CELLS)) + "건" +
               "".join(f" · {k} {n}건" for k, n in sorted(other.items())))
    measured_incidents = [r for r in selected if not r["fixtures_only"] and r.get("measurement_status") == "complete"
                          and r.get("truth") == "incident"]
    if measured_incidents:
        held = sum(bucket(r) == "판단 보류" for r in measured_incidents)
        unresolved = len(measured_incidents) - counts["TP"] - counts["FN"] - held
        st.write(f"실측 장애 {len(measured_incidents)}건 중 장애로 판정 {counts['TP']}건 · 정상으로 판정 {counts['FN']}건 · "
                 f"판단 보류 {held}건" + (f" · 기타 미완료 {unresolved}건" if unresolved else ""))
    if protocol == PROTOCOL:
        if report != "전체" and (campaign := next((r for r in rows if r["report"] == report and r.get("campaign_mix")), None)):
            st.caption(f"100회 목표 · 계획 시드 {campaign['campaign_seed']} · " +
                       " / ".join(f"{IMPACTS[k]} {n}건" for k, n in campaign["campaign_mix"].items()))
        show_tier_metrics(selected)
        show_decisions(selected)
    choices = ["전체", *CELLS, *sorted(other)]
    if st.session_state.get("cm_detail") not in choices:
        st.session_state.cm_detail = "전체"
    pick = st.selectbox("확인할 실험", choices, key="cm_detail")
    subset = [r for r in selected if pick == "전체" or bucket(r) == pick]
    if not subset:
        st.caption("이 조건에 해당하는 실험이 없습니다.")
    page = st.number_input("상세 기록 페이지", 1, max(1, (len(subset) + 9) // 10), 1) if len(subset) > 10 else 1
    show_rows(subset[(page - 1) * 10:page * 10])
    with st.expander("집계 기준"):
        st.write("각 관측 묶음마다 정상 Service 주소로 4회 독립 호출합니다. 실패율이 5%를 넘거나 p95가 300ms를 넘는 묶음이 하나라도 있으면 실제 장애입니다. 한 사례의 반복이나 부하 단계는 모두 합쳐 1건으로 셉니다.")
        st.write("재현율 = TP/(TP+FN), 미탐률 = FN/(TP+FN), 오탐률 = FP/(FP+TN), 정밀도 = TP/(TP+FP), F1 = 2TP/(2TP+FP+FN). 분모가 0이면 계산 불가입니다.")
        st.write("판정 완료율은 모델 판정을 요청한 실행 중 실측 정답과 이진 판정이 모두 확보된 비율입니다. 앱 재시도로 끝난 사례와 실측 전용 실행은 모델 판정 분모에서 제외합니다.")
