"""본사 관제 화면 (PRD 4.1 + 초안 승인).

- KPI 카드 5개: 총 재고 / 평균 정시출고율(목표 대비) / 미납 이벤트 건수 / 이상 센터 수 [PRD 4.1.1] + 승인 대기 초안 [보완]
- 탭 3개: 현황 개요 / KPI 추이 / 지시 관리
- 초안 승인 = ``db.approve_draft`` → WF_INSTRUCTION 생성. 사람만 누른다(CLAUDE.md 원칙 1).
- 이 모듈은 SQL 을 쓰지 않는다. 쓰기는 ``db.approve_draft / reject_draft / approve_report / send_instruction`` 뿐이다.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from logiwise import db, rules, settings
from logiwise.db import WorkflowError

from .common import (
    INSTRUCTION_ICON, LEVEL_BG, STATUS_BG, STATUS_ICON,
    flash, fmt_rate, render_table, show_flash, source_badge, status_badge, style_rows_by,
)

# 현황 개요 센터 카드 한 줄에 놓을 개수
_CARDS_PER_ROW = 4


# ------------------------------------------------------------------ 데이터 준비
def _load_rows() -> list[dict[str, Any]]:
    """overview 행에 등급·병목·사유를 붙인 목록(등급 위험 우선, 정시출고율 낮은 순)."""
    rows = [rules.enrich(r) for r in db.overview()]
    rows.sort(key=lambda r: (rules.STATUS_RANK.get(r["status"], 9), r.get("on_time_rate") if r.get("on_time_rate") is not None else 999.0))
    return rows


def _avg_rate(rows: list[dict[str, Any]]) -> float | None:
    rates = [float(r["on_time_rate"]) for r in rows if r.get("on_time_rate") is not None]
    return sum(rates) / len(rates) if rates else None


# ------------------------------------------------------------------ KPI 카드
def _kpi_cards(rows: list[dict[str, Any]], waiting: list[dict[str, Any]]) -> None:
    target = float(settings.RULES["on_time_target"])
    avg = _avg_rate(rows)
    total_stock = sum(int(r.get("stock_qty") or 0) for r in rows)
    shortage_events = sum(int(r.get("open_shortage_count") or 0) for r in rows)
    abnormal_centers = sum(1 for r in rows if r["status"] in (rules.STATUS_WARN, rules.STATUS_DANGER))

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("전국 총 재고량", f"{total_stock:,}")
    c2.metric(
        f"평균 정시출고율 (목표 {target:g}%)",
        fmt_rate(avg),
        delta=(f"{avg - target:+.1f}p 목표 대비" if avg is not None else None),
    )
    c3.metric("미납 이벤트 건수", f"{shortage_events}건")
    c4.metric("이상 발생 센터 수", f"{abnormal_centers}곳", help="주의+위험 등급 센터 수 [보완]")
    c5.metric("승인 대기 초안", f"{len(waiting)}건", help="에이전트/규칙이 만든 지시 초안. 지시 관리 탭에서 승인·반려")


# ------------------------------------------------------------------ 탭 1: 현황 개요
def _center_cards(rows: list[dict[str, Any]]) -> None:
    for i in range(0, len(rows), _CARDS_PER_ROW):
        cols = st.columns(_CARDS_PER_ROW)
        for col, r in zip(cols, rows[i:i + _CARDS_PER_ROW]):
            bg = STATUS_BG.get(r["status"], "#e9ecef")
            bns = ", ".join(r.get("bottlenecks") or []) or "없음"
            col.markdown(
                f"""<div style="background:{bg};border-radius:8px;padding:10px 12px;margin-bottom:8px;color:#1f1f1f;">
<b>{STATUS_ICON.get(r['status'], '⚪')} {r['center_name']}</b> <span style="color:#666">({r['center_code']})</span><br>
정시출고율 <b>{fmt_rate(r.get('on_time_rate'))}</b> · 재고 {int(r.get('stock_qty') or 0):,}<br>
미처리 이벤트 {int(r.get('open_event_count') or 0)}건 · 미완료 지시 {int(r.get('open_instruction_count') or 0)}건<br>
<span style="color:#444">병목: {bns}</span>
</div>""",
                unsafe_allow_html=True,
            )


def _ranking_table(rows: list[dict[str, Any]]) -> None:
    ranked = sorted(rows, key=lambda r: -(r.get("on_time_rate") if r.get("on_time_rate") is not None else -1))
    df = pd.DataFrame([
        {
            "순위": i + 1,
            "상태": status_badge(r["status"]),
            "센터": f"{r['center_name']} ({r['center_code']})",
            "정시출고율(%)": r.get("on_time_rate"),
            "총 재고": r.get("stock_qty"),
            "미납": r.get("shortage_qty"),
            "미입고": r.get("unreceived_qty"),
            "상태이상": r.get("anomaly_count"),
            "미처리 이벤트": r.get("open_event_count"),
            "기한초과": r.get("overdue_event_count"),
            "미완료 지시": r.get("open_instruction_count"),
            "병목": ", ".join(r.get("bottlenecks") or []),
            "_status": r["status"],
        }
        for i, r in enumerate(ranked)
    ])
    if df.empty:
        st.caption("센터 데이터가 없습니다.")
        return
    styler = style_rows_by(df, "_status", STATUS_BG).hide(axis="columns", subset=["_status"])
    render_table(df, styler)


def _tab_overview(rows: list[dict[str, Any]]) -> None:
    st.markdown("#### 센터별 상태")
    _center_cards(rows)
    st.markdown("#### 센터 성과 랭킹 (정시출고율 기준)")
    _ranking_table(rows)


# ------------------------------------------------------------------ 탭 2: KPI 추이
def _tab_trend(rows: list[dict[str, Any]]) -> None:
    name_by_code = {r["center_code"]: r["center_name"] for r in rows}
    kpi = db.kpi_trend(days=7)
    if not kpi:
        st.caption("최근 7일 KPI 가 없습니다.")
        return
    df = pd.DataFrame(kpi)
    df["센터"] = df["CENTER_CODE"].map(lambda c: f"{name_by_code.get(c, c)} ({c})")
    target = float(settings.RULES["on_time_target"])

    st.markdown("#### 센터별 정시출고율 (최근 7일)")
    fig = px.line(df, x="KPI_DATE", y="ON_TIME_RATE", color="센터", markers=True,
                  labels={"KPI_DATE": "날짜", "ON_TIME_RATE": "정시출고율(%)"})
    fig.add_hline(y=target, line_dash="dash", line_color="red", annotation_text=f"목표 {target:g}%")
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")

    st.markdown("#### 센터별 미납수량 (최근 7일)")
    fig2 = px.bar(df, x="KPI_DATE", y="SHORTAGE_QTY", color="센터", barmode="group",
                  labels={"KPI_DATE": "날짜", "SHORTAGE_QTY": "미납수량"})
    fig2.update_layout(height=340, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig2, width="stretch")

    st.markdown("#### 이상건수 히트맵 (0건 녹 / 1~3건 노 / 4건+ 적)")
    pivot = df.pivot_table(index="센터", columns="KPI_DATE", values="ANOMALY_COUNT", aggfunc="sum").fillna(0).astype(int)
    pivot = pivot.reset_index()
    date_cols = [c for c in pivot.columns if c != "센터"]

    def _cell(v: Any) -> str:
        return f"background-color: {LEVEL_BG[rules.heatmap_level(v)]}"

    styler = pivot.style.map(_cell, subset=date_cols)
    render_table(pivot, styler)


# ------------------------------------------------------------------ 탭 3: 지시 관리
def _draft_forms(waiting: list[dict[str, Any]]) -> None:
    st.markdown("#### 승인 대기 초안")
    st.caption("제목·내용·우선순위를 고친 뒤 승인하면 지시(WF_INSTRUCTION)가 생성된다. 승인은 사람만 한다.")
    if not waiting:
        st.info("승인 대기 초안이 없습니다. 에이전트 콘솔에서 사이클을 실행하면 생성됩니다.")
        return
    priorities = list(db.PRIORITIES)
    for d in waiting:
        label = f"[{source_badge(d['SOURCE'])}] {d['CENTER_NAME']} ({d['CENTER_CODE']}) — {d['TITLE']}"
        with st.expander(label, expanded=True):
            st.caption(f"초안 #{d['DRAFT_ID']} · 실행 #{d['RUN_ID']} · 생성 {d['CREATED_AT']} · 근거 {len(d['EVIDENCE_IDS'])}건")
            if d["EVIDENCE_IDS"]:
                st.code(", ".join(d["EVIDENCE_IDS"]), language=None)
            with st.form(key=f"draft_form_{d['DRAFT_ID']}"):
                title = st.text_input("제목", value=d["TITLE"], max_chars=120)
                body = st.text_area("내용", value=d["BODY"], height=220, max_chars=3000)
                pri_idx = priorities.index(d["PRIORITY"]) if d["PRIORITY"] in priorities else 0
                priority = st.selectbox("우선순위", priorities, index=pri_idx)
                note = st.text_input("검토 메모 (반려 사유 또는 수정 메모)", value="")
                c1, c2 = st.columns(2)
                approve = c1.form_submit_button("✅ 승인 (지시 발송)", type="primary", width="stretch")
                reject = c2.form_submit_button("❌ 반려", width="stretch")
            if approve:
                try:
                    iid = db.approve_draft(d["DRAFT_ID"], title=title, body=body, priority=priority, note=note or None)
                    flash("success", f"초안 #{d['DRAFT_ID']} 승인 → 지시 #{iid} 발송 (상태: 지시완료)")
                except WorkflowError as e:
                    flash("error", f"승인 실패: {e}")
                st.rerun()
            if reject:
                try:
                    db.reject_draft(d["DRAFT_ID"], note=note or None)
                    flash("warning", f"초안 #{d['DRAFT_ID']} 반려")
                except WorkflowError as e:
                    flash("error", f"반려 실패: {e}")
                st.rerun()


def _instruction_history() -> None:
    st.markdown("#### 지시 이력")
    instr = db.instructions()
    if not instr:
        st.caption("지시 이력이 없습니다.")
        return
    df = pd.DataFrame([
        {
            "ID": i["INSTRUCTION_ID"],
            "상태": f"{INSTRUCTION_ICON.get(i['STATUS'], '')} {i['STATUS']}",
            "센터": f"{i['CENTER_NAME']} ({i['CENTER_CODE']})",
            "제목": i["TITLE"],
            "우선순위": i["PRIORITY"],
            "출처": i["SOURCE"],
            "발송": i["CREATED_AT"],
            "확인": i.get("ACKNOWLEDGED_AT") or "",
            "보고": i.get("REPORTED_AT") or "",
            "승인": i.get("APPROVED_AT") or "",
            "보고 건수": len(i.get("REPORTS") or []),
        }
        for i in instr
    ])
    render_table(df)

    # 조치중 지시: 센터 보고 내용 확인 + 승인 버튼 (PRD 4.1.4)
    in_action = [i for i in instr if i["STATUS"] == "조치중"]
    st.markdown("#### 센터 보고 승인 (조치중 → 완료)")
    if not in_action:
        st.caption("승인 대기 중인 센터 보고가 없습니다.")
    for i in in_action:
        with st.expander(f"🔧 #{i['INSTRUCTION_ID']} {i['CENTER_NAME']} — {i['TITLE']}", expanded=True):
            st.markdown(f"**지시 내용**  \n{i['BODY']}")
            for rep in i.get("REPORTS") or []:
                st.markdown(f"**센터 보고** ({rep['ACTION_TYPE']}, {rep['REPORTED_AT']}, {rep['REPORTED_BY']})  \n{rep['REPORT_BODY']}")
            if st.button("✅ 보고 승인 → 완료", key=f"approve_report_{i['INSTRUCTION_ID']}", type="primary"):
                try:
                    db.approve_report(i["INSTRUCTION_ID"])
                    flash("success", f"지시 #{i['INSTRUCTION_ID']} 보고 승인 → 완료")
                except WorkflowError as e:
                    flash("error", f"보고 승인 실패: {e}")
                st.rerun()


def _send_form(rows: list[dict[str, Any]]) -> None:
    st.markdown("#### 지시 직접 발송")
    codes = [r["center_code"] for r in sorted(rows, key=lambda r: r["center_code"])]
    names = {r["center_code"]: r["center_name"] for r in rows}
    with st.form(key="send_instruction_form", clear_on_submit=True):
        center = st.selectbox("대상 센터", codes, format_func=lambda c: f"{names.get(c, c)} ({c})")
        title = st.text_input("제목", max_chars=120)
        body = st.text_area("내용", height=140, max_chars=3000)
        priority = st.selectbox("우선순위", list(db.PRIORITIES))
        submitted = st.form_submit_button("📤 발송", type="primary")
    if submitted:
        try:
            iid = db.send_instruction(center, title, body, priority=priority)
            flash("success", f"지시 #{iid} 발송 완료 ({names.get(center, center)}, 상태: 지시완료)")
        except WorkflowError as e:
            flash("error", f"발송 실패: {e}")
        st.rerun()


def _tab_instructions(rows: list[dict[str, Any]], waiting: list[dict[str, Any]]) -> None:
    _draft_forms(waiting)
    st.divider()
    _instruction_history()
    st.divider()
    _send_form(rows)


# ------------------------------------------------------------------ 진입점
def render() -> None:
    """본사 관제 화면 전체."""
    st.title("🏢 본사 관제")
    show_flash()
    rows = _load_rows()
    waiting = db.drafts(status="대기")
    _kpi_cards(rows, waiting)
    tab1, tab2, tab3 = st.tabs(["현황 개요", "KPI 추이", "지시 관리"])
    with tab1:
        _tab_overview(rows)
    with tab2:
        _tab_trend(rows)
    with tab3:
        _tab_instructions(rows, waiting)
