"""Step 5 화면 테스트: streamlit.testing.v1.AppTest 로 세 화면을 렌더한다.

- 실제 Claude 는 호출하지 않는다. 콘솔에서는 **규칙 기반 버튼만** 누른다.
- ``db_path`` 픽스처가 LOGIWISE_DB_PATH 를 임시 DB 로 바꾸므로 app.py 도 그 DB 를 쓴다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from logiwise import db, monitor

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"
VIEWS_DIR = Path(__file__).resolve().parent.parent / "views"


def _app() -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=60)


def _assert_no_exception(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _buttons(at: AppTest) -> list:
    """폼 제출 버튼을 포함한 모든 버튼."""
    return list(at.button)


def _labels(at: AppTest) -> list[str]:
    return [b.label for b in _buttons(at)]


# ------------------------------------------------------------------ 세 화면 렌더
def test_hq_view_renders(db_path):
    at = _app().run()
    _assert_no_exception(at)
    assert at.sidebar.radio[0].value == "본사 관제"
    assert len(at.metric) == 5                              # KPI 카드 5개
    assert "승인 대기 초안" in at.metric[4].label
    assert [t.label for t in at.tabs][:3] == ["현황 개요", "KPI 추이", "지시 관리"]   # at.tabs 는 Tab 블록의 평면 리스트
    # 센터 카드 아이콘(🔴 1 · 🟡 2) 이 본문에 나타난다
    body = "\n".join(m.value for m in at.markdown)
    assert "🔴" in body and "🟡" in body and "🟢" in body
    assert any("📤 발송" in lbl for lbl in _labels(at))     # 직접 발송 폼


def test_center_view_renders_with_banner(db_path):
    at = _app()
    at.run()
    at.sidebar.radio[0].set_value("센터 업무").run()
    _assert_no_exception(at)
    assert at.sidebar.selectbox[0].value == "C001"
    # C003 은 seed 에 미완료 지시(지시완료) 1건 → 경고 배너
    at.sidebar.selectbox[0].set_value("C003").run()
    _assert_no_exception(at)
    assert at.error and any("미완료 지시" in e.value for e in at.error)
    assert len(at.metric) == 4                              # 상태 카드 4개
    body = "\n".join(m.value for m in at.markdown)
    assert "⚠ 병목" in body                                  # C003 은 보관·출고 병목
    assert any("확인했습니다" in lbl for lbl in _labels(at))
    assert at.number_input                                   # 조치 등록 폼의 수량 입력


def test_center_view_without_open_instruction_has_no_banner(db_path):
    at = _app()
    at.run()
    at.sidebar.radio[0].set_value("센터 업무").run()
    _assert_no_exception(at)
    assert at.sidebar.selectbox[0].value == "C001"
    assert not any("미완료 지시" in e.value for e in at.error)


def test_agent_console_renders(db_path):
    at = _app()
    at.run()
    at.sidebar.radio[0].set_value("에이전트 콘솔").run()
    _assert_no_exception(at)
    labels = _labels(at)
    assert any("에이전트 사이클 실행" in lbl for lbl in labels)
    assert any("규칙 기반 실행" in lbl for lbl in labels)
    assert at.chat_input                                    # 운영 질의 채팅 입력
    assert db.runs(path=db_path) == []                       # 렌더만으로는 실행 기록이 생기지 않는다


# ------------------------------------------------------------------ 본사: 승인 버튼
def test_hq_has_approve_button_when_draft_waiting(db_path):
    res = monitor.run_cycle(trigger="test", use_agent=False, path=db_path)
    assert res.status == "success" and len(res.drafts) == 2
    at = _app().run()
    _assert_no_exception(at)
    approve = [b for b in _buttons(at) if "승인" in b.label and "지시 발송" in b.label]
    assert len(approve) == 2                                # 대기 초안 2건 → 승인 버튼 2개
    assert any("반려" in lbl for lbl in _labels(at))
    assert at.metric[4].value == "2건"
    assert any("승인 대기 초안 2건" in w.value for w in at.sidebar.warning)


def test_hq_approve_draft_creates_instruction(db_path):
    monitor.run_cycle(trigger="test", use_agent=False, path=db_path)
    waiting = db.drafts(status="대기", path=db_path)
    n_before = len(db.instructions(path=db_path))
    at = _app().run()
    approve = [b for b in _buttons(at) if "승인" in b.label and "지시 발송" in b.label]
    approve[0].click().run()
    _assert_no_exception(at)
    assert len(db.drafts(status="대기", path=db_path)) == len(waiting) - 1
    instr = db.instructions(path=db_path)
    assert len(instr) == n_before + 1 and instr[0]["SOURCE"] == "에이전트초안" and instr[0]["STATUS"] == "지시완료"
    assert any("승인" in s.value for s in at.success)


# ------------------------------------------------------------------ 콘솔: 규칙 버튼 → AGENT_RUN
def test_console_rules_button_creates_agent_run(db_path):
    at = _app()
    at.run()
    at.sidebar.radio[0].set_value("에이전트 콘솔").run()
    at.button(key="btn_rules").click().run()
    _assert_no_exception(at)
    runs = db.runs(path=db_path)
    assert len(runs) == 1
    assert runs[0]["TRIGGER"] == "console" and runs[0]["MODE"] == "rules" and runs[0]["STATUS"] == "success"
    assert len(db.drafts(status="대기", path=db_path)) == 2
    body = "\n".join(m.value for m in at.markdown)
    assert "rules" in body and f"실행 #{runs[0]['RUN_ID']}" in body
    # 에이전트 버튼은 존재하되 누르지 않는다 (실제 Claude 호출 금지)
    assert at.button(key="btn_agent") is not None


# ------------------------------------------------------------------ 불변식 I3: 뷰에 SQL 없음
@pytest.mark.parametrize("name", ["hq.py", "center.py", "agent_console.py", "common.py"])
def test_views_have_no_sql(name):
    src = (VIEWS_DIR / name).read_text(encoding="utf-8")
    assert "execute(" not in src
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\s+(INTO|FROM|\w+\s+SET)\b", src)
    assert "sqlite3" not in src
