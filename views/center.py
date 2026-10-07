"""센터 업무 화면 (PRD 4.2).

- 미완료 지시 배너 [4.2.1] / 상태 카드 4개 [4.2.2] / 입출고 플로우 5단계 + 병목 강조 [4.2.3]
- 7일 입출고 그룹 바 [4.2.4] / 이벤트 리스트(지연색) + 조치 등록 폼 [4.2.5] / 지시 아코디언 [4.2.6]
- 쓰기는 ``db.resolve_event / acknowledge_instruction / submit_report`` 뿐이다. SQL 없음.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from logiwise import db, rules
from logiwise.db import WorkflowError

from .common import INSTRUCTION_ICON, LEVEL_BG, flash, fmt_rate, render_table, show_flash, status_badge, style_rows_by

# 입출고 일별 집계 컬럼 ↔ 프로세스 단계 (rules.STAGES 순서)
_STAGE_COLUMN: dict[str, str] = {
    "입고": "IN_QTY", "보관": "STORAGE_QTY", "피킹": "PICKING_QTY", "패킹": "PACKING_QTY", "출고": "OUT_QTY",
}


# ------------------------------------------------------------------ 데이터
def _center_row(center_code: str) -> dict[str, Any] | None:
    for r in db.overview():
        if r["center_code"] == center_code:
            return rules.enrich(r)
    return None


def _action_types() -> list[str]:
    codes = [c["CODE"] for c in db.etc_codes("ACTION_TYPE")]
    return codes or ["재출고", "대체출고", "입고확인", "거래처독촉", "재검수", "폐기", "보류"]


# ------------------------------------------------------------------ 배너·카드
def _banner(open_instr: list[dict[str, Any]]) -> None:
    """미완료 지시가 있으면 상단 경고 배너 (PRD 4.2.1)."""
    if not open_instr:
        st.success("본사 미완료 지시가 없습니다.")
        return
    lines = [f"🚨 **본사 미완료 지시 {len(open_instr)}건** — 아래 '본사 지시' 에서 확인·보고하세요."]
    for i in open_instr:
        lines.append(f"- {INSTRUCTION_ICON.get(i['STATUS'], '')} [{i['PRIORITY']}] #{i['INSTRUCTION_ID']} {i['TITLE']} ({i['STATUS']})")
    st.error("\n".join(lines))


def _status_cards(row: dict[str, Any]) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("보관 SKU 수", f"{int(row.get('sku_count') or 0):,}")
    c2.metric("출고 진행 건", f"{int(row.get('outbound_wip') or 0):,}")
    c3.metric("미납 이벤트", f"{int(row.get('open_shortage_count') or 0)}건", help=f"미납 수량 {int(row.get('shortage_qty') or 0)}")
    c4.metric("상태이상 건수", f"{int(row.get('open_anomaly_count') or 0)}건")


# ------------------------------------------------------------------ 플로우·추이
def _process_flow(row: dict[str, Any], inout: list[dict[str, Any]]) -> None:
    """입고 → 보관 → 피킹 → 패킹 → 출고. 병목 단계는 빨강 + ⚠ (PRD 4.2.3)."""
    st.markdown("#### 입출고 프로세스 플로우")
    latest = inout[-1] if inout else {}
    flags = rules.bottleneck_flags(row)
    day = latest.get("INOUT_DATE", "-")
    st.caption(f"기준일 {day} 단계별 처리량. 병목 기준: 미입고>3 입고 / 상태이상>0 보관 / 미납>5 출고 (PRD 4.2.3)")
    cols = st.columns(len(rules.STAGES))
    for col, stage in zip(cols, rules.STAGES):
        qty = int(latest.get(_STAGE_COLUMN[stage], 0) or 0)
        is_bn = bool(flags.get(stage))
        bg = "#e53935" if is_bn else "#eef2f7"
        fg = "#fff" if is_bn else "#222"
        badge = "<br><b>⚠ 병목</b>" if is_bn else "<br>&nbsp;"
        col.markdown(
            f"""<div style="background:{bg};color:{fg};border-radius:10px;padding:12px 8px;text-align:center;">
<div style="font-size:0.9rem">{stage}</div>
<div style="font-size:1.4rem;font-weight:700">{qty:,}</div>{badge}
</div>""",
            unsafe_allow_html=True,
        )
    bns = row.get("bottlenecks") or []
    if bns:
        st.warning(f"⚠ 병목 단계: {', '.join(bns)} — 사유: " + "; ".join(row.get("reasons") or []))


def _inout_chart(inout: list[dict[str, Any]]) -> None:
    st.markdown("#### 최근 7일 입출고")
    if not inout:
        st.caption("입출고 집계가 없습니다.")
        return
    df = pd.DataFrame(inout)
    long = df.melt(id_vars=["INOUT_DATE"], value_vars=["IN_QTY", "OUT_QTY"], var_name="구분", value_name="수량")
    long["구분"] = long["구분"].map({"IN_QTY": "입고", "OUT_QTY": "출고"})
    fig = px.bar(long, x="INOUT_DATE", y="수량", color="구분", barmode="group", labels={"INOUT_DATE": "날짜"})
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")


# ------------------------------------------------------------------ 이벤트 조치
def _event_table(events: list[dict[str, Any]]) -> None:
    st.markdown("#### 처리 필요 이벤트")
    if not events:
        st.info("미처리 이벤트가 없습니다.")
        return
    df = pd.DataFrame([
        {
            "ID": e["EVENT_ID"],
            "유형": e["EVENT_TYPE"],
            "세부": e.get("ANOMALY_TYPE") or "",
            "상품": e.get("PRODUCT_NAME") or e.get("PRODUCT_CODE") or "",
            "수량": e["EVENT_QTY"],
            "처리": e["RESOLVED_QTY"],
            "잔여": e["REMAIN_QTY"],
            "상태": e["STATUS"],
            "발생": e["OCCURRED_AT"],
            "기한": e["DUE_AT"],
            "지연(h)": round(rules.event_delay_hours(e["DUE_AT"]), 1),
            "메모": e.get("NOTE") or "",
            "_level": rules.event_delay_level(e["DUE_AT"]),
        }
        for e in events
    ])
    st.caption("행 색: 기한 이내 녹 / 지연 24h 미만 노 / 24h 이상 적 [보완]")
    styler = style_rows_by(df, "_level", LEVEL_BG).hide(axis="columns", subset=["_level"])
    render_table(df, styler)


def _event_form(events: list[dict[str, Any]]) -> None:
    """이벤트 선택 → 조치 유형 → 수량 → 등록 (PDA: 드롭다운 + number_input)."""
    st.markdown("#### 조치 등록")
    if not events:
        return
    by_id = {e["EVENT_ID"]: e for e in events}
    # 이벤트 선택은 폼 밖에 둬서 선택이 바뀌면 수량 상한(잔여)이 바로 반영되게 한다
    event_id = st.selectbox(
        "이벤트", list(by_id),
        format_func=lambda i: f"#{i} {by_id[i]['EVENT_TYPE']} {by_id[i].get('PRODUCT_NAME') or ''} (잔여 {by_id[i]['REMAIN_QTY']})",
        key="event_select",
    )
    remain = int(by_id[event_id]["REMAIN_QTY"])
    with st.form(key="resolve_event_form", clear_on_submit=True):
        action = st.selectbox("조치 유형", _action_types())
        qty = st.number_input("조치 수량", min_value=1, max_value=max(1, remain), value=max(1, remain), step=1)
        note = st.text_input("메모 (선택)")
        submitted = st.form_submit_button("📝 조치 등록", type="primary")
    if submitted:
        try:
            updated = db.resolve_event(event_id, action, int(qty), note=note or None)
            flash("success", f"이벤트 #{event_id} 조치 {int(qty)} 등록 → 상태 {updated.get('STATUS')}")
        except WorkflowError as e:
            flash("error", f"조치 등록 실패: {e}")
        st.rerun()


# ------------------------------------------------------------------ 본사 지시
def _instruction_accordion(instr: list[dict[str, Any]]) -> None:
    st.markdown("#### 본사 지시")
    if not instr:
        st.caption("받은 지시가 없습니다.")
        return
    action_types = _action_types()
    for i in instr:
        icon = INSTRUCTION_ICON.get(i["STATUS"], "")
        is_open = i["STATUS"] in db.OPEN_INSTRUCTION_STATUSES
        with st.expander(f"{icon} [{i['PRIORITY']}] #{i['INSTRUCTION_ID']} {i['TITLE']} — {i['STATUS']}", expanded=is_open):
            st.caption(f"출처 {i['SOURCE']} · 발송 {i['CREATED_AT']}"
                       + (f" · 확인 {i['ACKNOWLEDGED_AT']}" if i.get("ACKNOWLEDGED_AT") else "")
                       + (f" · 보고 {i['REPORTED_AT']}" if i.get("REPORTED_AT") else "")
                       + (f" · 승인 {i['APPROVED_AT']}" if i.get("APPROVED_AT") else ""))
            st.markdown(i["BODY"])
            for rep in i.get("REPORTS") or []:
                st.markdown(f"**조치 보고** ({rep['ACTION_TYPE']}, {rep['REPORTED_AT']})  \n{rep['REPORT_BODY']}")

            if i["STATUS"] == "지시완료":
                if st.button("👁️ 확인했습니다", key=f"ack_{i['INSTRUCTION_ID']}", type="primary"):
                    try:
                        db.acknowledge_instruction(i["INSTRUCTION_ID"])
                        flash("success", f"지시 #{i['INSTRUCTION_ID']} 확인 → 센터확인중")
                    except WorkflowError as e:
                        flash("error", f"확인 실패: {e}")
                    st.rerun()
            elif i["STATUS"] == "센터확인중":
                with st.form(key=f"report_form_{i['INSTRUCTION_ID']}"):
                    action = st.selectbox("조치 유형", action_types)
                    body = st.text_area("조치 결과", height=120, placeholder="조치 내용 · 처리 수량 · 잔여 수량 · 완료 예정 시각")
                    submitted = st.form_submit_button("🔧 결과 보고", type="primary")
                if submitted:
                    try:
                        db.submit_report(i["INSTRUCTION_ID"], action, body)
                        flash("success", f"지시 #{i['INSTRUCTION_ID']} 결과 보고 → 조치중 (본사 승인 대기)")
                    except WorkflowError as e:
                        flash("error", f"보고 실패: {e}")
                    st.rerun()
            elif i["STATUS"] == "조치중":
                st.info("본사 승인 대기 중입니다.")
            else:
                st.success("완료된 지시입니다.")


# ------------------------------------------------------------------ 진입점
def render(center_code: str) -> None:
    """센터 업무 화면 전체. ``center_code`` 는 사이드바에서 고른 센터."""
    row = _center_row(center_code)
    if row is None:
        st.error(f"센터 {center_code} 를 찾을 수 없습니다.")
        return
    st.title(f"🏭 센터 업무 — {row['center_name']} ({center_code})")
    show_flash()

    instr = db.instructions(center_code)
    open_instr = [i for i in instr if i["STATUS"] in db.OPEN_INSTRUCTION_STATUSES]
    _banner(open_instr)

    st.caption(f"등급 {status_badge(row['status'])} · 정시출고율 {fmt_rate(row.get('on_time_rate'))} · 기준일 {row.get('kpi_date') or '-'}")
    _status_cards(row)

    inout = db.inout_daily(center_code, days=7)
    _process_flow(row, inout)
    _inout_chart(inout)

    events = db.events(center_code, only_open=True)
    _event_table(events)
    _event_form(events)

    st.divider()
    _instruction_accordion(instr)
