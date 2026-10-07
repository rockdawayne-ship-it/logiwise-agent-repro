"""LOGIWISE Streamlit 진입점.

실행: ``.\\.venv\\Scripts\\python.exe -m streamlit run app.py``

- 첫 실행 시 ``db.init_schema`` + ``seed.seed``(데이터가 없을 때만).
- 사이드바: 화면 라디오(본사 관제 / 센터 업무 / 에이전트 콘솔) [PRD 9.2], 센터 선택(센터 업무용),
  승인 대기 초안 수 경고, 판단 기준 expander(출처 표시).
- 모든 쓰기는 ``db.py`` 함수로만 한다. 이 파일과 ``views/`` 에는 SQL 이 없다.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from logiwise import db, seed, settings
from views import agent_console, center, hq

PAGES = ("본사 관제", "센터 업무", "에이전트 콘솔")

# 판단 기준 표 (STEP0_PLAN 7장). 값은 config.toml [rules] 에서 읽는다.
_CRITERIA = [
    ("정시출고율 목표", "on_time_target", "%", "PRD 4.1.1"),
    ("입고 병목: 미입고수량 >", "unreceived_bottleneck", "", "PRD 4.2.3"),
    ("보관 병목: 상태이상 >", None, "0", "PRD 4.2.3"),
    ("출고 병목: 미납수량 >", "shortage_bottleneck", "", "PRD 4.2.3"),
    ("히트맵 노랑 최소 건수", "heatmap_yellow_min", "건", "PRD 4.1.3"),
    ("히트맵 빨강 최소 건수", "heatmap_red_min", "건", "PRD 4.1.3"),
    ("위험: 정시출고율 <", "danger_on_time", "%", "보완"),
    ("위험: 미처리 상태이상 ≥", "danger_anomaly_count", "건", "보완"),
    ("주의: 정시출고율 < 목표 또는 미처리 이벤트 ≥ 1건", None, "", "보완"),
    ("이벤트 지연 빨강: 기한 초과 ≥", "event_delay_hours", "h", "보완"),
    ("사이클당 분석 센터 수", None, str(settings.MONITOR.get("max_centers_per_cycle")), "보완"),
]


def _bootstrap() -> None:
    """세션당 한 번: 스키마 생성 + 샘플 데이터(없을 때만)."""
    if st.session_state.get("_bootstrapped"):
        return
    db.init_schema()
    if seed.seed():
        st.toast("샘플 데이터를 생성했습니다.")
    st.session_state["_bootstrapped"] = True


def _criteria_table() -> pd.DataFrame:
    rows = []
    for label, key, unit, source in _CRITERIA:
        if key is None:
            value = unit
        else:
            v = settings.RULES.get(key)
            value = f"{v:g}{unit}" if isinstance(v, (int, float)) else f"{v}{unit}"
        rows.append({"기준": label, "값": value, "출처": source})
    return pd.DataFrame(rows)


def _sidebar() -> tuple[str, str | None]:
    """사이드바를 그리고 (선택 화면, 선택 센터 코드) 를 돌려준다."""
    st.sidebar.title("LOGIWISE")
    st.sidebar.caption("자율 모니터링 AI Agent · 규칙 → 판단 → 승인")
    page = st.sidebar.radio("화면", PAGES, key="page")

    center_code: str | None = None
    if page == "센터 업무":
        centers = db.centers()
        codes = [c["CENTER_CODE"] for c in centers]
        names = {c["CENTER_CODE"]: c["CENTER_NAME"] for c in centers}
        if codes:
            center_code = st.sidebar.selectbox("센터 선택", codes, format_func=lambda c: f"{names[c]} ({c})", key="center_code")

    n_waiting = len(db.drafts(status="대기"))
    if n_waiting:
        st.sidebar.warning(f"⚠️ 승인 대기 초안 {n_waiting}건 — 본사 관제 › 지시 관리")
    else:
        st.sidebar.caption("승인 대기 초안 없음")

    with st.sidebar.expander("판단 기준 (출처)"):
        st.table(_criteria_table())
        st.caption("등급·병목·히트맵은 rules.py 가 계산한다. 에이전트는 이 값을 바꾸지 않는다.")

    st.sidebar.caption(f"DB: {settings.get_db_path()}")
    return page, center_code


def main() -> None:
    st.set_page_config(page_title="LOGIWISE 자율 모니터링", page_icon="🚚", layout="wide")
    _bootstrap()
    page, center_code = _sidebar()
    if page == "본사 관제":
        hq.render()
    elif page == "센터 업무":
        if center_code is None:
            st.error("센터 마스터가 비어 있습니다. 샘플 데이터를 생성하세요.")
        else:
            center.render(center_code)
    else:
        agent_console.render()


main()
