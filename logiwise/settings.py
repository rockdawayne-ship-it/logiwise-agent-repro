"""설정 로딩: config.toml, KST, DB 경로, 코드 자릿수 검증.

우선순위
- DB 경로: 환경변수 ``LOGIWISE_DB_PATH`` > ``config.toml [database].path`` (프로젝트 루트 기준)
- 코드 자릿수: PRD 7.2 (CENTER 4, VENDOR 6, PRODUCT 10). SQLite 는 CHAR 길이를 강제하지 않으므로
  Python ``zfill`` 로 보정·검증한다.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------ 경로
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# ------------------------------------------------------------------ 시간대
KST = timezone(timedelta(hours=9), name="KST")
DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def now_kst() -> datetime:
    """현재 시각(KST, tz-aware)."""
    return datetime.now(tz=KST)


def today_kst() -> str:
    """오늘 날짜 문자열(KST, 'YYYY-MM-DD')."""
    return now_kst().strftime(DATE_FMT)


def ts(dt: datetime | None = None) -> str:
    """DB 저장용 시각 문자열('YYYY-MM-DD HH:MM:SS'). 인자가 없으면 지금(KST)."""
    return (dt or now_kst()).strftime(DATETIME_FMT)


# ------------------------------------------------------------------ 설정 파일
def load_config(path: Path | None = None) -> dict[str, Any]:
    """config.toml 을 읽어 dict 로 돌려준다. 파일이 없으면 빈 섹션들을 돌려준다."""
    p = path or CONFIG_PATH
    if not p.exists():
        return {"database": {}, "agent": {}, "monitor": {}, "rules": {}}
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    for section in ("database", "agent", "monitor", "rules"):
        cfg.setdefault(section, {})
    return cfg


CONFIG: dict[str, Any] = load_config()

# 판단 기준. PRD 출처와 보완값을 config.toml 에 둔다. 코드에는 기본값만.
_RULE_DEFAULTS: dict[str, float | int] = {
    "on_time_target": 97.0,        # PRD 4.1.1
    "unreceived_bottleneck": 3,    # PRD 4.2.3
    "shortage_bottleneck": 5,      # PRD 4.2.3
    "heatmap_yellow_min": 1,       # PRD 4.1.3
    "heatmap_red_min": 4,          # PRD 4.1.3
    "danger_on_time": 95.0,        # 보완
    "danger_anomaly_count": 4,     # 보완
    "event_delay_hours": 24,       # 보완
}
RULES: dict[str, float | int] = {**_RULE_DEFAULTS, **CONFIG.get("rules", {})}

AGENT: dict[str, Any] = {
    "model": "claude-sonnet-5-5",
    "effort": "medium",
    "max_turns": 12,
    "timeout_seconds": 180,
    **CONFIG.get("agent", {}),
}

MONITOR: dict[str, Any] = {
    "interval_seconds": 300,
    "max_centers_per_cycle": 2,
    "use_agent": True,
    **CONFIG.get("monitor", {}),
}


# ------------------------------------------------------------------ DB 경로
def get_db_path() -> str:
    """DB 파일 경로. 환경변수 ``LOGIWISE_DB_PATH`` 가 있으면 그것을, 없으면 config.toml 값을 쓴다.

    호출할 때마다 환경변수를 다시 읽으므로 테스트에서 monkeypatch 로 바꿀 수 있다.
    """
    env = os.environ.get("LOGIWISE_DB_PATH")
    if env:
        return env
    rel = CONFIG.get("database", {}).get("path", "data/logiwise.db")
    p = Path(rel)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


# 임포트 시점의 값. 가능한 한 get_db_path() 를 쓴다.
DB_PATH: str = get_db_path()


# ------------------------------------------------------------------ 코드 검증
class CodeError(ValueError):
    """코드 자릿수/형식 위반."""


# (자릿수, 접두 문자). 접두 문자가 있으면 숫자만 들어온 입력은 접두 + zfill(자릿수-1) 로 보정한다.
CODE_SPEC: dict[str, tuple[int, str]] = {
    "CENTER": (4, "C"),
    "VENDOR": (6, "V"),
    "PRODUCT": (10, ""),
}


def normalize_code(kind: str, value: Any) -> str:
    """코드를 PRD 7.2 자릿수로 보정하고 검증한다.

    - ``normalize_code("CENTER", "c3")`` → ``"C003"``
    - ``normalize_code("VENDOR", 1)`` → ``"V00001"``
    - ``normalize_code("PRODUCT", "1")`` → ``"0000000001"``
    - 자릿수를 넘거나 형식이 다르면 :class:`CodeError`.
    """
    key = kind.upper()
    if key not in CODE_SPEC:
        raise CodeError(f"알 수 없는 코드 종류: {kind}")
    width, prefix = CODE_SPEC[key]

    if value is None:
        raise CodeError(f"{key} 코드가 비어 있음")
    raw = str(value).strip().upper()
    if not raw:
        raise CodeError(f"{key} 코드가 비어 있음")

    if prefix:
        digits = raw[1:] if raw.startswith(prefix) else raw
        if not digits.isdigit():
            raise CodeError(f"{key} 코드 형식 오류: {value!r} (예: {prefix}{'1'.zfill(width - 1)})")
        code = prefix + digits.zfill(width - 1)
    else:
        if not raw.isdigit():
            raise CodeError(f"{key} 코드 형식 오류: {value!r} (숫자 {width}자리)")
        code = raw.zfill(width)

    if len(code) != width:
        raise CodeError(f"{key} 코드 자릿수 오류: {value!r} → {code!r} ({width}자리 필요)")
    pattern = rf"^{re.escape(prefix)}\d{{{width - len(prefix)}}}$"
    if not re.match(pattern, code):
        raise CodeError(f"{key} 코드 형식 오류: {code!r}")
    return code
