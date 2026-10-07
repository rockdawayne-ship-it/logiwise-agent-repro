"""세 화면이 같이 쓰는 표시 상수·헬퍼.

색·아이콘 값만 둔다. 등급·병목·지연 판정은 ``rules.py`` 가 하고, 여기서는 그 결과를 색으로 바꿀 뿐이다.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from logiwise import rules

# 센터 등급 아이콘 (PRD 4.1.2: 정상 🟢 / 주의 🟡 / 위험 🔴)
STATUS_ICON: dict[str, str] = {
    rules.STATUS_NORMAL: "🟢",
    rules.STATUS_WARN: "🟡",
    rules.STATUS_DANGER: "🔴",
    rules.STATUS_NO_DATA: "⚪",
}

# 등급별 행·카드 배경색
STATUS_BG: dict[str, str] = {
    rules.STATUS_NORMAL: "#dff5df",
    rules.STATUS_WARN: "#fff3cd",
    rules.STATUS_DANGER: "#f8d7da",
    rules.STATUS_NO_DATA: "#e9ecef",
}

# 히트맵·이벤트 지연색 (녹/노/적)
LEVEL_BG: dict[str, str] = {
    rules.LEVEL_GREEN: "#c6efce",
    rules.LEVEL_YELLOW: "#ffeb9c",
    rules.LEVEL_RED: "#ffc7ce",
}

# 지시 상태 아이콘 (PRD 4.1.4: 📤👀🔧✅)
INSTRUCTION_ICON: dict[str, str] = {
    "지시완료": "📤",
    "센터확인중": "👀",
    "조치중": "🔧",
    "완료": "✅",
}

# 초안·실행 출처 배지 (CLAUDE.md 원칙 8: rules 를 AI 결과와 구분)
SOURCE_LABEL: dict[str, str] = {"agent": "🤖 agent", "rules": "📐 rules"}

_FLASH_KEY = "_flash"


def flash(kind: str, message: str) -> None:
    """다음 rerun 에서 보여 줄 한 줄 메시지를 저장한다. kind: success / error / info / warning."""
    st.session_state[_FLASH_KEY] = (kind, message)


def show_flash() -> None:
    """저장된 메시지를 한 번 보여 주고 지운다."""
    item = st.session_state.pop(_FLASH_KEY, None)
    if not item:
        return
    kind, message = item
    getattr(st, kind, st.info)(message)


def status_badge(status: str) -> str:
    """'🔴 위험' 처럼 아이콘+등급 문자열."""
    return f"{STATUS_ICON.get(status, '⚪')} {status}"


def source_badge(source: str | None) -> str:
    return SOURCE_LABEL.get(str(source), str(source))


def fmt_rate(value: Any) -> str:
    """정시출고율 표시. None 이면 '-'."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


def style_rows_by(df: pd.DataFrame, key_col: str, palette: dict[str, str]):
    """``key_col`` 값에 따라 행 배경색을 입힌 Styler. ``key_col`` 은 표에 그대로 남는다."""
    def _row(row: pd.Series) -> list[str]:
        bg = palette.get(str(row[key_col]), "")
        css = f"background-color: {bg}; color: #1f1f1f" if bg else ""
        return [css] * len(row)

    return df.style.apply(_row, axis=1)


def render_table(df: pd.DataFrame, styler=None, **kwargs: Any) -> None:
    """DataFrame 또는 Styler 를 표로. 빈 표는 안내 문구."""
    if df.empty:
        st.caption("표시할 데이터가 없습니다.")
        return
    st.dataframe(styler if styler is not None else df, width="stretch", hide_index=True, **kwargs)
