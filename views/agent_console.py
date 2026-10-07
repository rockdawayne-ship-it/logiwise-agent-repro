"""에이전트 콘솔 화면 [보완: PRD 에 없는 세 번째 화면].

- "에이전트 사이클 실행": ``monitor.run_cycle(trigger="console", use_agent=True)`` — 실제 Claude 호출, 비용 발생.
- "규칙 기반 실행": ``monitor.run_cycle(trigger="console", use_agent=False)`` — LLM 미사용, 화면에 ``rules`` 표시.
- 마지막 사이클 결과 / 실행 이력 + 상세(보고서·탐지·도구 로그·질문) / 운영 질의 채팅(``agent.chat``, session_id 유지).
- 이 화면에는 승인·발송 버튼이 없다. 초안 승인은 본사 관제 › 지시 관리에서만 한다.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from logiwise import agent, db, monitor
from logiwise.agent import AgentUnavailable

from .common import render_table, show_flash, source_badge, status_badge

_LAST_CYCLE = "last_cycle"
_CHAT_MESSAGES = "chat_messages"
_CHAT_SESSION = "chat_session_id"

_RUN_STATUS_ICON = {"success": "✅", "failed": "❌", "skipped": "⏭️", "running": "⏳"}


# ------------------------------------------------------------------ 사이클 실행
def _run(use_agent: bool) -> None:
    label = "에이전트(실제 Claude)" if use_agent else "규칙 기반"
    with st.spinner(f"{label} 사이클 실행 중…"):
        res = monitor.run_cycle(trigger="console", use_agent=use_agent)
    st.session_state[_LAST_CYCLE] = res


def _run_buttons() -> None:
    c1, c2 = st.columns(2)
    with c1:
        st.caption("🤖 실제 Claude 를 호출한다. **비용이 발생**하며 Claude Code 로그인이 필요하다.")
        if st.button("🤖 에이전트 사이클 실행", key="btn_agent", type="primary", width="stretch"):
            _run(use_agent=True)
    with c2:
        st.caption("📐 LLM 을 쓰지 않는 대체 경로. 결과에 `rules` 로 표시된다.")
        if st.button("📐 규칙 기반 실행", key="btn_rules", width="stretch"):
            _run(use_agent=False)


# ------------------------------------------------------------------ 마지막 사이클 결과
def _detected_table(detected: list[dict[str, Any]]) -> None:
    if not detected:
        st.caption("탐지된 주의·위험 센터가 없습니다.")
        return
    df = pd.DataFrame([
        {
            "센터": f"{d.get('center_name') or ''} ({d['center_code']})",
            "등급": status_badge(d["status"]),
            "정시출고율": d.get("on_time_rate"),
            "병목": ", ".join(d.get("bottlenecks") or []),
            "탐지 사유": "; ".join(d.get("reasons") or []),
            "처리": ("🎯 " if d.get("disposition") == monitor.DISPOSITION_TARGET else "⏭️ ") + str(d.get("disposition")),
            "지문": d.get("fingerprint"),
        }
        for d in detected
    ])
    render_table(df)


def _report_view(report: dict[str, Any] | None, source: str) -> None:
    if not report:
        st.caption("보고서 없음(실패 또는 skipped).")
        return
    st.markdown(f"**요약** · 출처 {source_badge(source)}")
    st.text(report.get("summary", ""))
    findings = report.get("findings") or []
    st.markdown(f"**진단 (findings) {len(findings)}건**")
    for f in findings:
        with st.expander(f"{status_badge(f['severity'])} {f['center_code']}", expanded=True):
            st.markdown(f"- **관측**: {f['observation']}\n- **가설**: {f['hypothesis']}\n- **권장**: {f['recommendation']}")
            st.caption("근거: " + ", ".join(f.get("evidence_ids") or []))
    drafts = report.get("instruction_drafts") or []
    st.markdown(f"**지시 초안 {len(drafts)}건** — 본사 관제 › 지시 관리에서 승인·반려")
    for d in drafts:
        with st.expander(f"[{d['priority']}] {d['center_code']} — {d['title']}"):
            st.text(d["body"])
            st.caption("근거: " + ", ".join(d.get("evidence_ids") or []))


def _last_cycle() -> None:
    st.markdown("### 마지막 사이클 결과")
    res = st.session_state.get(_LAST_CYCLE)
    if res is None:
        st.info("아직 이 세션에서 실행한 사이클이 없습니다. 위 버튼으로 실행하세요.")
        return
    icon = _RUN_STATUS_ICON.get(res.status, "")
    st.markdown(f"**실행 #{res.run_id}** · {icon} `{res.status}` · 출처 {source_badge(res.mode)} · 기준일 {res.kpi_day or '-'} · "
                f"탐지 {len(res.detected)} / 대상 {len(res.targets)} / 건너뜀 {len(res.skipped)} / 초안 {len(res.drafts)}")
    if res.error:
        st.error(f"실패 사유: {res.error}")
    if res.status == "skipped":
        st.warning("대상 센터가 없어 분석을 건너뛰었습니다(모두 중복 또는 탐지 없음).")
    st.markdown("**탐지 결과** (🎯 대상 / ⏭️ 건너뜀: 중복=같은 지문의 대기 초안 있음, 한도=사이클당 센터 수 초과)")
    _detected_table(res.detected)
    if res.targets:
        st.caption("대상: " + ", ".join(res.targets))
    if res.skipped:
        st.caption("건너뜀: " + ", ".join(f"{c}({r})" for c, r in res.skipped.items()))
    _report_view(res.report.model_dump() if res.report is not None else None, res.mode)


# ------------------------------------------------------------------ 실행 이력
def _history() -> None:
    st.markdown("### 실행 이력")
    runs = db.runs(limit=20)
    if not runs:
        st.caption("실행 기록이 없습니다.")
        return
    df = pd.DataFrame([
        {
            "RUN_ID": r["RUN_ID"],
            "시작": r["STARTED_AT"],
            "트리거": r["TRIGGER"],
            "출처": source_badge(r["MODE"]),
            "상태": f"{_RUN_STATUS_ICON.get(r['STATUS'], '')} {r['STATUS']}",
            "대상": ", ".join(r.get("SCOPE") or []),
            "탐지": len(r.get("DETECTED") or []),
            "초안": len((r.get("REPORT") or {}).get("instruction_drafts") or []),
            "모델": r.get("MODEL") or "",
            "턴": r.get("NUM_TURNS"),
            "비용(USD)": r.get("COST_USD"),
            "소요(ms)": r.get("DURATION_MS"),
            "오류": r.get("ERROR_MESSAGE") or "",
        }
        for r in runs
    ])
    render_table(df)

    run_ids = [r["RUN_ID"] for r in runs]
    sel = st.selectbox("상세 보기", run_ids, format_func=lambda i: f"#{i}", key="run_detail_select")
    run = db.get_run(sel)
    if not run:
        return
    t1, t2, t3, t4 = st.tabs(["보고서", "탐지", "도구 로그", "질문"])
    with t1:
        if run.get("REPORT"):
            _report_view(run["REPORT"], run["MODE"])
            with st.expander("보고서 JSON"):
                st.json(run["REPORT"])
        else:
            st.caption("보고서 없음" + (f" — {run['ERROR_MESSAGE']}" if run.get("ERROR_MESSAGE") else ""))
    with t2:
        _detected_table(run.get("DETECTED") or [])
        alerts = db.alerts(sel)
        if alerts:
            st.caption("MON_ALERT 기록")
            render_table(pd.DataFrame([
                {"센터": a["CENTER_CODE"], "등급": a["CENTER_STATUS"], "대상": "예" if a["TARGETED"] else "아니오",
                 "건너뜀 사유": a.get("SKIP_REASON") or "", "사유": "; ".join(a.get("REASONS") or []),
                 "지문": a["FINGERPRINT"], "탐지 시각": a["DETECTED_AT"]}
                for a in alerts
            ]))
    with t3:
        log_rows = run.get("TOOL_LOG") or []
        if log_rows:
            render_table(pd.DataFrame([
                {"시각": e.get("at"), "도구": e.get("tool"), "인자": json.dumps(e.get("args") or {}, ensure_ascii=False),
                 "근거 ID 수": e.get("n_ids"), "성공": e.get("ok", True)}
                for e in log_rows
            ]))
        else:
            st.caption("도구 호출 기록 없음(규칙 기반 실행은 도구를 쓰지 않는다).")
    with t4:
        st.code(run.get("QUESTION") or "(질문 없음)", language=None)


# ------------------------------------------------------------------ 운영 질의 채팅
def _chat() -> None:
    st.markdown("### 운영 질의 (Claude, 읽기 전용 도구)")
    st.caption("실제 Claude 호출(비용 발생). 등급·병목은 규칙 값을 그대로 인용하며, 발송·승인은 할 수 없다.")
    messages: list[dict[str, str]] = st.session_state.setdefault(_CHAT_MESSAGES, [])
    sid = st.session_state.get(_CHAT_SESSION)
    c1, c2 = st.columns([3, 1])
    c1.caption(f"session_id: `{sid}`" if sid else "session_id: (새 대화)")
    if c2.button("대화 초기화", key="btn_chat_reset"):
        st.session_state[_CHAT_MESSAGES] = []
        st.session_state[_CHAT_SESSION] = None
        st.rerun()

    for m in messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    question = st.chat_input("예: C003 센터 정시출고율 하락 원인은?")
    if not question:
        return
    messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Claude 가 조회 중…"):
            try:
                text, new_sid = agent.chat(question, session_id=sid)
                st.session_state[_CHAT_SESSION] = new_sid
            except AgentUnavailable as e:
                text = f"⚠️ 응답 불가 — {e.reason_ko}"
        st.markdown(text)
    messages.append({"role": "assistant", "content": text})


# ------------------------------------------------------------------ 진입점
def render() -> None:
    st.title("🤖 에이전트 콘솔")
    show_flash()
    _run_buttons()
    st.divider()
    _last_cycle()
    st.divider()
    _history()
    st.divider()
    _chat()
