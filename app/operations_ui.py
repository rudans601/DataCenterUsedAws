"""기존 Prometheus에 연결된 Grafana에서 실험 전후를 확인한다."""
import streamlit as st

from app.detection_ui import dashboard_period, dashboard_url
from kubeops_copilot.detection import report_rows
from kubeops_copilot.scenarios import NAMES


@st.fragment
def render(directory):
    base = dashboard_url()
    if not base:
        st.info("Grafana 주소를 KUBEOPS_GRAFANA_URL에 설정하면 대시보드가 표시됩니다.")
        return
    periods = {"최근 30분": "from=now-30m&to=now&refresh=15s"}
    for row in report_rows(directory):
        period = dashboard_period(row)
        if period:
            start, _, query = period
            label = f"{start:%Y-%m-%d %H:%M:%S} · {NAMES[row['case']]} · 시드 {row['seed']}"
            periods[label] = query
    choice = st.selectbox("표시할 시간대", periods.keys())
    url = f"{base}?{periods[choice]}"
    st.link_button("Grafana 원본 열기", url)
    if hasattr(st, "iframe"):
        st.iframe(url + "&kiosk", height=780)
    else:
        import streamlit.components.v1 as components
        components.iframe(url + "&kiosk", height=780, scrolling=True)
