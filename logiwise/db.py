"""DB 연결·조회·업무 트랜잭션.

원칙
- 모든 쓰기는 이 모듈의 함수로만 한다. 뷰에서 SQL 을 직접 쓰지 않는다.
- 워크플로우 상태 전환은 PRD 5 순서만 허용한다: 지시완료 → 센터확인중 → 조치중 → 완료.
  잘못된 전환은 :class:`WorkflowError`.
- 모든 함수는 마지막 키워드 인자 ``path`` 로 DB 경로를 받는다. 생략하면 ``settings.get_db_path()``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import settings
from .settings import normalize_code, ts

# PRD 7.1 의 12개 테이블 + 추가 3개
TABLES: tuple[str, ...] = (
    "MT_CENTER", "MT_VENDOR", "MT_PRODUCT", "MT_ETC_CODE",
    "INV_CENTER_STOCK", "INV_CENTER_INOUT_HISTORY", "INV_CENTER_INOUT_DAILY",
    "AN_CENTER_KPI_DAILY",
    "EXC_CENTER_EVENT", "EXC_CENTER_EVENT_RESOLVE",
    "WF_INSTRUCTION", "WF_ACTION_REPORT",
    "AGENT_RUN", "MON_ALERT", "WF_DRAFT",
)

# 워크플로우 상태 전환표 (PRD 5)
INSTRUCTION_FLOW: dict[str, str] = {
    "지시완료": "센터확인중",
    "센터확인중": "조치중",
    "조치중": "완료",
}
INSTRUCTION_STATUSES: tuple[str, ...] = ("지시완료", "센터확인중", "조치중", "완료")
OPEN_INSTRUCTION_STATUSES: tuple[str, ...] = ("지시완료", "센터확인중", "조치중")
EVENT_OPEN_STATUSES: tuple[str, ...] = ("미처리", "부분처리")
PRIORITIES: tuple[str, ...] = ("보통", "높음", "긴급")
DRAFT_STATUSES: tuple[str, ...] = ("대기", "승인", "반려")
RUN_STATUSES: tuple[str, ...] = ("running", "success", "failed", "skipped")


class WorkflowError(Exception):
    """허용되지 않은 상태 전환 또는 업무 규칙 위반."""


# ------------------------------------------------------------------ 연결
@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """with 블록이 정상 종료하면 commit, 예외면 rollback, 항상 close.

    외래키를 켜고 row_factory 를 sqlite3.Row 로 둔다.
    """
    db_path = path or settings.get_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(path: str | None = None) -> None:
    """schema.sql 을 적용한다(CREATE TABLE IF NOT EXISTS 이므로 여러 번 호출해도 안전)."""
    sql = settings.SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(sql)


def table_names(path: str | None = None) -> list[str]:
    """현재 DB 의 테이블 이름 목록."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


# ------------------------------------------------------------------ 내부 유틸
def _rows(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    """조회 결과를 dict 리스트로."""
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _date_from(days: int) -> str:
    """오늘 포함 최근 ``days`` 일의 시작 날짜 문자열."""
    from datetime import timedelta

    return (settings.now_kst() - timedelta(days=days - 1)).strftime(settings.DATE_FMT)


# ------------------------------------------------------------------ 조회
def centers(path: str | None = None) -> list[dict[str, Any]]:
    """센터 마스터 전체."""
    with connect(path) as conn:
        return _rows(conn, "SELECT * FROM MT_CENTER ORDER BY CENTER_CODE")


def overview(path: str | None = None) -> list[dict[str, Any]]:
    """센터별 현황 한 줄 요약.

    센터마다 최신 KPI 행 + 미처리 이벤트 집계 + 미완료 지시 수를 합친다.
    컬럼(소문자): center_code, center_name, region, kpi_date, on_time_rate, stock_qty, sku_count,
    outbound_wip, unreceived_qty, shortage_qty, anomaly_count,
    open_event_count, open_shortage_count, open_unreceived_count, open_anomaly_count,
    open_shortage_qty, open_unreceived_qty, overdue_event_count, open_instruction_count,
    in_qty, out_qty (최신 입출고 집계일 기준)
    KPI 가 한 건도 없는 센터는 kpi_date 가 None 이다.
    """
    now = ts()
    sql = """
    WITH latest_kpi AS (
        SELECT k.* FROM AN_CENTER_KPI_DAILY k
        JOIN (SELECT CENTER_CODE, MAX(KPI_DATE) AS KPI_DATE FROM AN_CENTER_KPI_DAILY GROUP BY CENTER_CODE) m
          ON m.CENTER_CODE = k.CENTER_CODE AND m.KPI_DATE = k.KPI_DATE
    ),
    latest_inout AS (
        SELECT d.* FROM INV_CENTER_INOUT_DAILY d
        JOIN (SELECT CENTER_CODE, MAX(INOUT_DATE) AS INOUT_DATE FROM INV_CENTER_INOUT_DAILY GROUP BY CENTER_CODE) m
          ON m.CENTER_CODE = d.CENTER_CODE AND m.INOUT_DATE = d.INOUT_DATE
    ),
    ev AS (
        SELECT CENTER_CODE,
               COUNT(*)                                                        AS open_event_count,
               SUM(CASE WHEN EVENT_TYPE='미납' THEN 1 ELSE 0 END)              AS open_shortage_count,
               SUM(CASE WHEN EVENT_TYPE='미입고' THEN 1 ELSE 0 END)            AS open_unreceived_count,
               SUM(CASE WHEN EVENT_TYPE='상태이상' THEN 1 ELSE 0 END)          AS open_anomaly_count,
               SUM(CASE WHEN EVENT_TYPE='미납' THEN EVENT_QTY-RESOLVED_QTY ELSE 0 END)   AS open_shortage_qty,
               SUM(CASE WHEN EVENT_TYPE='미입고' THEN EVENT_QTY-RESOLVED_QTY ELSE 0 END) AS open_unreceived_qty,
               SUM(CASE WHEN DUE_AT < ? THEN 1 ELSE 0 END)                     AS overdue_event_count
        FROM EXC_CENTER_EVENT
        WHERE STATUS IN ('미처리','부분처리')
        GROUP BY CENTER_CODE
    ),
    wf AS (
        SELECT CENTER_CODE, COUNT(*) AS open_instruction_count
        FROM WF_INSTRUCTION WHERE STATUS IN ('지시완료','센터확인중','조치중')
        GROUP BY CENTER_CODE
    )
    SELECT c.CENTER_CODE AS center_code, c.CENTER_NAME AS center_name, c.REGION AS region,
           k.KPI_DATE AS kpi_date, k.ON_TIME_RATE AS on_time_rate,
           COALESCE(k.STOCK_QTY,0) AS stock_qty, COALESCE(k.SKU_COUNT,0) AS sku_count,
           COALESCE(k.OUTBOUND_WIP,0) AS outbound_wip,
           COALESCE(k.UNRECEIVED_QTY,0) AS unreceived_qty, COALESCE(k.SHORTAGE_QTY,0) AS shortage_qty,
           COALESCE(k.ANOMALY_COUNT,0) AS anomaly_count,
           COALESCE(ev.open_event_count,0) AS open_event_count,
           COALESCE(ev.open_shortage_count,0) AS open_shortage_count,
           COALESCE(ev.open_unreceived_count,0) AS open_unreceived_count,
           COALESCE(ev.open_anomaly_count,0) AS open_anomaly_count,
           COALESCE(ev.open_shortage_qty,0) AS open_shortage_qty,
           COALESCE(ev.open_unreceived_qty,0) AS open_unreceived_qty,
           COALESCE(ev.overdue_event_count,0) AS overdue_event_count,
           COALESCE(wf.open_instruction_count,0) AS open_instruction_count,
           COALESCE(io.IN_QTY,0) AS in_qty, COALESCE(io.OUT_QTY,0) AS out_qty
    FROM MT_CENTER c
    LEFT JOIN latest_kpi k ON k.CENTER_CODE = c.CENTER_CODE
    LEFT JOIN latest_inout io ON io.CENTER_CODE = c.CENTER_CODE
    LEFT JOIN ev ON ev.CENTER_CODE = c.CENTER_CODE
    LEFT JOIN wf ON wf.CENTER_CODE = c.CENTER_CODE
    ORDER BY c.CENTER_CODE
    """
    with connect(path) as conn:
        return _rows(conn, sql, (now,))


def kpi_trend(center_code: str | None = None, days: int = 7, path: str | None = None) -> list[dict[str, Any]]:
    """최근 ``days`` 일 센터별 일별 KPI (센터코드·날짜 오름차순)."""
    since = _date_from(days)
    sql = "SELECT * FROM AN_CENTER_KPI_DAILY WHERE KPI_DATE >= ?"
    params: list[Any] = [since]
    if center_code:
        sql += " AND CENTER_CODE = ?"
        params.append(normalize_code("CENTER", center_code))
    sql += " ORDER BY CENTER_CODE, KPI_DATE"
    with connect(path) as conn:
        return _rows(conn, sql, params)


def inout_daily(center_code: str | None = None, days: int = 7, path: str | None = None) -> list[dict[str, Any]]:
    """최근 ``days`` 일 일별 입출고 집계."""
    since = _date_from(days)
    sql = "SELECT * FROM INV_CENTER_INOUT_DAILY WHERE INOUT_DATE >= ?"
    params: list[Any] = [since]
    if center_code:
        sql += " AND CENTER_CODE = ?"
        params.append(normalize_code("CENTER", center_code))
    sql += " ORDER BY CENTER_CODE, INOUT_DATE"
    with connect(path) as conn:
        return _rows(conn, sql, params)


def events(center_code: str | None = None, only_open: bool = True, path: str | None = None) -> list[dict[str, Any]]:
    """예외 이벤트 목록. 기본은 미처리·부분처리만. 기한 빠른 순."""
    sql = """
    SELECT e.*, (e.EVENT_QTY - e.RESOLVED_QTY) AS REMAIN_QTY, p.PRODUCT_NAME
    FROM EXC_CENTER_EVENT e LEFT JOIN MT_PRODUCT p ON p.PRODUCT_CODE = e.PRODUCT_CODE
    WHERE 1=1
    """
    params: list[Any] = []
    if only_open:
        sql += " AND e.STATUS IN ('미처리','부분처리')"
    if center_code:
        sql += " AND e.CENTER_CODE = ?"
        params.append(normalize_code("CENTER", center_code))
    sql += " ORDER BY e.DUE_AT, e.EVENT_ID"
    with connect(path) as conn:
        return _rows(conn, sql, params)


def event_resolves(event_id: int, path: str | None = None) -> list[dict[str, Any]]:
    """이벤트 하나의 조치 이력."""
    with connect(path) as conn:
        return _rows(conn, "SELECT * FROM EXC_CENTER_EVENT_RESOLVE WHERE EVENT_ID=? ORDER BY RESOLVE_ID", (event_id,))


def instructions(center_code: str | None = None, status: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
    """본사 지시 목록(최신순). 각 행에 REPORTS(조치 보고 리스트)를 붙인다."""
    sql = "SELECT i.*, c.CENTER_NAME FROM WF_INSTRUCTION i JOIN MT_CENTER c ON c.CENTER_CODE=i.CENTER_CODE WHERE 1=1"
    params: list[Any] = []
    if center_code:
        sql += " AND i.CENTER_CODE = ?"
        params.append(normalize_code("CENTER", center_code))
    if status:
        sql += " AND i.STATUS = ?"
        params.append(status)
    sql += " ORDER BY i.INSTRUCTION_ID DESC"
    with connect(path) as conn:
        rows = _rows(conn, sql, params)
        for r in rows:
            r["REPORTS"] = _rows(conn, "SELECT * FROM WF_ACTION_REPORT WHERE INSTRUCTION_ID=? ORDER BY REPORT_ID", (r["INSTRUCTION_ID"],))
    return rows


def get_instruction(instruction_id: int, path: str | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        return _one(conn, "SELECT * FROM WF_INSTRUCTION WHERE INSTRUCTION_ID=?", (instruction_id,))


def drafts(status: str | None = "대기", center_code: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
    """지시 초안 목록. 기본은 대기 중만. EVIDENCE_IDS 는 리스트로 풀어 준다."""
    sql = "SELECT d.*, c.CENTER_NAME FROM WF_DRAFT d JOIN MT_CENTER c ON c.CENTER_CODE=d.CENTER_CODE WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND d.STATUS = ?"
        params.append(status)
    if center_code:
        sql += " AND d.CENTER_CODE = ?"
        params.append(normalize_code("CENTER", center_code))
    sql += " ORDER BY d.DRAFT_ID DESC"
    with connect(path) as conn:
        rows = _rows(conn, sql, params)
    for r in rows:
        r["EVIDENCE_IDS"] = _json_loads(r.get("EVIDENCE_IDS_JSON"), [])
    return rows


def get_draft(draft_id: int, path: str | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        r = _one(conn, "SELECT * FROM WF_DRAFT WHERE DRAFT_ID=?", (draft_id,))
    if r:
        r["EVIDENCE_IDS"] = _json_loads(r.get("EVIDENCE_IDS_JSON"), [])
    return r


def runs(limit: int = 20, path: str | None = None) -> list[dict[str, Any]]:
    """에이전트 실행 기록(최신순). JSON 컬럼은 파싱해 REPORT/TOOL_LOG/DETECTED/SCOPE 로 붙인다."""
    with connect(path) as conn:
        rows = _rows(conn, "SELECT * FROM AGENT_RUN ORDER BY RUN_ID DESC LIMIT ?", (limit,))
    for r in rows:
        r["SCOPE"] = _json_loads(r.get("SCOPE_CODES"), [])
        r["REPORT"] = _json_loads(r.get("REPORT_JSON"), None)
        r["TOOL_LOG"] = _json_loads(r.get("TOOL_LOG_JSON"), [])
        r["DETECTED"] = _json_loads(r.get("DETECTED_JSON"), [])
    return rows


def get_run(run_id: int, path: str | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        r = _one(conn, "SELECT * FROM AGENT_RUN WHERE RUN_ID=?", (run_id,))
    if r:
        r["SCOPE"] = _json_loads(r.get("SCOPE_CODES"), [])
        r["REPORT"] = _json_loads(r.get("REPORT_JSON"), None)
        r["TOOL_LOG"] = _json_loads(r.get("TOOL_LOG_JSON"), [])
        r["DETECTED"] = _json_loads(r.get("DETECTED_JSON"), [])
    return r


def alerts(run_id: int | None = None, path: str | None = None) -> list[dict[str, Any]]:
    """탐지 기록. run_id 를 주면 그 실행분만."""
    sql = "SELECT * FROM MON_ALERT WHERE 1=1"
    params: list[Any] = []
    if run_id is not None:
        sql += " AND RUN_ID = ?"
        params.append(run_id)
    sql += " ORDER BY ALERT_ID DESC"
    with connect(path) as conn:
        rows = _rows(conn, sql, params)
    for r in rows:
        r["REASONS"] = _json_loads(r.get("REASONS_JSON"), [])
    return rows


def etc_codes(code_group: str, path: str | None = None) -> list[dict[str, Any]]:
    """공통코드 그룹 조회."""
    with connect(path) as conn:
        return _rows(conn, "SELECT * FROM MT_ETC_CODE WHERE CODE_GROUP=? AND USE_YN='Y' ORDER BY SORT_ORDER", (code_group,))


# ------------------------------------------------------------------ 업무 트랜잭션: 지시 워크플로우
def _insert_instruction(conn: sqlite3.Connection, center_code: str, title: str, body: str,
                        priority: str, source: str, draft_id: int | None, created_by: str) -> int:
    """연결을 공유하는 내부 INSERT. 초안 승인과 직접 발송이 같이 쓴다."""
    code = normalize_code("CENTER", center_code)
    if not title or not title.strip():
        raise WorkflowError("지시 제목이 비어 있음")
    if not body or not body.strip():
        raise WorkflowError("지시 내용이 비어 있음")
    if priority not in PRIORITIES:
        raise WorkflowError(f"우선순위 값 오류: {priority!r} (허용: {PRIORITIES})")
    if not _one(conn, "SELECT 1 FROM MT_CENTER WHERE CENTER_CODE=?", (code,)):
        raise WorkflowError(f"존재하지 않는 센터: {code}")
    cur = conn.execute(
        """INSERT INTO WF_INSTRUCTION (CENTER_CODE, TITLE, BODY, PRIORITY, STATUS, SOURCE, DRAFT_ID, CREATED_BY, CREATED_AT)
           VALUES (?,?,?,?,'지시완료',?,?,?,?)""",
        (code, title.strip(), body.strip(), priority, source, draft_id, created_by, ts()),
    )
    return int(cur.lastrowid)


def send_instruction(center_code: str, title: str, body: str, priority: str = "보통",
                     source: str = "본사직접", created_by: str = "본사", path: str | None = None) -> int:
    """본사가 지시를 직접 발송한다. 상태 '지시완료'. 반환: INSTRUCTION_ID."""
    with connect(path) as conn:
        return _insert_instruction(conn, center_code, title, body, priority, source, None, created_by)


def _transition(conn: sqlite3.Connection, instruction_id: int, expected: str) -> dict[str, Any]:
    """현재 상태가 ``expected`` 인지 확인하고 다음 상태를 돌려준다. 아니면 WorkflowError."""
    row = _one(conn, "SELECT * FROM WF_INSTRUCTION WHERE INSTRUCTION_ID=?", (instruction_id,))
    if row is None:
        raise WorkflowError(f"존재하지 않는 지시: {instruction_id}")
    current = row["STATUS"]
    if current != expected:
        raise WorkflowError(
            f"지시 {instruction_id} 는 '{current}' 상태라 '{expected} → {INSTRUCTION_FLOW.get(expected, '?')}' 전환을 할 수 없음"
        )
    row["NEXT_STATUS"] = INSTRUCTION_FLOW[expected]
    return row


def acknowledge_instruction(instruction_id: int, path: str | None = None) -> str:
    """센터 '확인했습니다': 지시완료 → 센터확인중."""
    with connect(path) as conn:
        row = _transition(conn, instruction_id, "지시완료")
        conn.execute("UPDATE WF_INSTRUCTION SET STATUS=?, ACKNOWLEDGED_AT=? WHERE INSTRUCTION_ID=?",
                     (row["NEXT_STATUS"], ts(), instruction_id))
        return row["NEXT_STATUS"]


def submit_report(instruction_id: int, action_type: str, report_body: str,
                  reported_by: str = "센터", path: str | None = None) -> int:
    """센터 조치 결과 보고: 센터확인중 → 조치중. WF_ACTION_REPORT 1건 생성. 반환: REPORT_ID."""
    if not report_body or not report_body.strip():
        raise WorkflowError("보고 내용이 비어 있음")
    if not action_type:
        raise WorkflowError("조치 유형이 비어 있음")
    with connect(path) as conn:
        row = _transition(conn, instruction_id, "센터확인중")
        now = ts()
        cur = conn.execute(
            "INSERT INTO WF_ACTION_REPORT (INSTRUCTION_ID, ACTION_TYPE, REPORT_BODY, REPORTED_BY, REPORTED_AT) VALUES (?,?,?,?,?)",
            (instruction_id, action_type, report_body.strip(), reported_by, now),
        )
        conn.execute("UPDATE WF_INSTRUCTION SET STATUS=?, REPORTED_AT=? WHERE INSTRUCTION_ID=?",
                     (row["NEXT_STATUS"], now, instruction_id))
        return int(cur.lastrowid)


def approve_report(instruction_id: int, approved_by: str = "본사", path: str | None = None) -> str:
    """본사 보고 승인: 조치중 → 완료. 미승인 보고에 승인 시각을 적는다."""
    with connect(path) as conn:
        row = _transition(conn, instruction_id, "조치중")
        now = ts()
        conn.execute("UPDATE WF_ACTION_REPORT SET APPROVED_AT=?, APPROVED_BY=? WHERE INSTRUCTION_ID=? AND APPROVED_AT IS NULL",
                     (now, approved_by, instruction_id))
        conn.execute("UPDATE WF_INSTRUCTION SET STATUS=?, APPROVED_AT=? WHERE INSTRUCTION_ID=?",
                     (row["NEXT_STATUS"], now, instruction_id))
        return row["NEXT_STATUS"]


# ------------------------------------------------------------------ 업무 트랜잭션: 이벤트 조치
def resolve_event(event_id: int, action_type: str, action_qty: int, note: str | None = None,
                  resolved_by: str = "센터", path: str | None = None) -> dict[str, Any]:
    """이벤트 조치 등록.

    - 남은 수량보다 적으면 '부분처리', 정확히 채우면 '처리완료'.
    - 남은 수량을 초과하거나 0 이하, 이미 처리완료면 WorkflowError (DB 는 바뀌지 않음).
    반환: 갱신된 이벤트 행.
    """
    try:
        qty = int(action_qty)
    except (TypeError, ValueError):
        raise WorkflowError(f"조치 수량이 정수가 아님: {action_qty!r}")
    if qty <= 0:
        raise WorkflowError("조치 수량은 1 이상이어야 함")
    if not action_type:
        raise WorkflowError("조치 유형이 비어 있음")
    with connect(path) as conn:
        ev = _one(conn, "SELECT * FROM EXC_CENTER_EVENT WHERE EVENT_ID=?", (event_id,))
        if ev is None:
            raise WorkflowError(f"존재하지 않는 이벤트: {event_id}")
        if ev["STATUS"] not in EVENT_OPEN_STATUSES:
            raise WorkflowError(f"이벤트 {event_id} 는 이미 '{ev['STATUS']}' 상태")
        remain = ev["EVENT_QTY"] - ev["RESOLVED_QTY"]
        if qty > remain:
            raise WorkflowError(f"조치 수량 {qty} 이 남은 수량 {remain} 을 초과함")
        now = ts()
        conn.execute(
            "INSERT INTO EXC_CENTER_EVENT_RESOLVE (EVENT_ID, ACTION_TYPE, ACTION_QTY, ACTION_NOTE, RESOLVED_BY, RESOLVED_AT) VALUES (?,?,?,?,?,?)",
            (event_id, action_type, qty, note, resolved_by, now),
        )
        new_resolved = ev["RESOLVED_QTY"] + qty
        new_status = "처리완료" if new_resolved >= ev["EVENT_QTY"] else "부분처리"
        conn.execute(
            "UPDATE EXC_CENTER_EVENT SET RESOLVED_QTY=?, STATUS=?, RESOLVED_AT=? WHERE EVENT_ID=?",
            (new_resolved, new_status, now if new_status == "처리완료" else None, event_id),
        )
        return _one(conn, "SELECT * FROM EXC_CENTER_EVENT WHERE EVENT_ID=?", (event_id,)) or {}


# ------------------------------------------------------------------ 업무 트랜잭션: 초안
def add_draft(center_code: str, title: str, body: str, priority: str, fingerprint: str,
              evidence_ids: list[str] | None = None, run_id: int | None = None,
              source: str = "agent", path: str | None = None) -> int:
    """지시 초안을 대기열에 넣는다. 반환: DRAFT_ID."""
    code = normalize_code("CENTER", center_code)
    if priority not in PRIORITIES:
        raise WorkflowError(f"우선순위 값 오류: {priority!r}")
    if not title or not body:
        raise WorkflowError("초안 제목/내용이 비어 있음")
    if not fingerprint:
        raise WorkflowError("초안 지문(fingerprint)이 비어 있음")
    with connect(path) as conn:
        cur = conn.execute(
            """INSERT INTO WF_DRAFT (RUN_ID, CENTER_CODE, TITLE, BODY, PRIORITY, EVIDENCE_IDS_JSON, FINGERPRINT, SOURCE, STATUS, CREATED_AT)
               VALUES (?,?,?,?,?,?,?,?,'대기',?)""",
            (run_id, code, title.strip(), body.strip(), priority,
             json.dumps(list(evidence_ids or []), ensure_ascii=False), fingerprint, source, ts()),
        )
        return int(cur.lastrowid)


def _load_waiting_draft(conn: sqlite3.Connection, draft_id: int) -> dict[str, Any]:
    d = _one(conn, "SELECT * FROM WF_DRAFT WHERE DRAFT_ID=?", (draft_id,))
    if d is None:
        raise WorkflowError(f"존재하지 않는 초안: {draft_id}")
    if d["STATUS"] != "대기":
        raise WorkflowError(f"초안 {draft_id} 는 이미 '{d['STATUS']}' 처리됨")
    return d


def approve_draft(draft_id: int, title: str | None = None, body: str | None = None,
                  priority: str | None = None, decided_by: str = "본사", note: str | None = None,
                  path: str | None = None) -> int:
    """초안 승인 = WF_INSTRUCTION 생성(사람만 한다). 같은 트랜잭션에서 초안을 '승인' 으로 바꾼다.

    제목·내용·우선순위를 넘기면 수정본으로 발송한다. 대기 상태가 아니면 WorkflowError.
    반환: 생성된 INSTRUCTION_ID.
    """
    with connect(path) as conn:
        d = _load_waiting_draft(conn, draft_id)
        instruction_id = _insert_instruction(
            conn, d["CENTER_CODE"],
            title if title is not None else d["TITLE"],
            body if body is not None else d["BODY"],
            priority if priority is not None else d["PRIORITY"],
            "에이전트초안", draft_id, decided_by,
        )
        conn.execute(
            "UPDATE WF_DRAFT SET STATUS='승인', DECIDED_AT=?, DECIDED_BY=?, DECISION_NOTE=?, INSTRUCTION_ID=? WHERE DRAFT_ID=?",
            (ts(), decided_by, note, instruction_id, draft_id),
        )
        return instruction_id


def reject_draft(draft_id: int, note: str | None = None, decided_by: str = "본사", path: str | None = None) -> None:
    """초안 반려. 대기 상태가 아니면 WorkflowError."""
    with connect(path) as conn:
        _load_waiting_draft(conn, draft_id)
        conn.execute(
            "UPDATE WF_DRAFT SET STATUS='반려', DECIDED_AT=?, DECIDED_BY=?, DECISION_NOTE=? WHERE DRAFT_ID=?",
            (ts(), decided_by, note, draft_id),
        )


# ------------------------------------------------------------------ 업무 트랜잭션: 실행 기록
def start_run(trigger: str, mode: str = "agent", model: str | None = None, question: str | None = None,
              scope_codes: list[str] | None = None, path: str | None = None) -> int:
    """AGENT_RUN 에 running 행을 만든다. 반환: RUN_ID."""
    if mode not in ("agent", "rules"):
        raise WorkflowError(f"실행 모드 오류: {mode!r}")
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO AGENT_RUN (TRIGGER, MODE, STATUS, MODEL, QUESTION, SCOPE_CODES, STARTED_AT) VALUES (?,?,'running',?,?,?,?)",
            (trigger, mode, model, question, json.dumps(list(scope_codes or []), ensure_ascii=False), ts()),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, report: Any = None, tool_log: Any = None, detected: Any = None,
               num_turns: int | None = None, cost_usd: float | None = None, duration_ms: int | None = None,
               error_message: str | None = None, session_id: str | None = None, model: str | None = None,
               question: str | None = None, scope_codes: list[str] | None = None,
               path: str | None = None) -> None:
    """실행을 마감한다(success/failed/skipped). running 이 아닌 실행은 WorkflowError."""
    if status not in RUN_STATUSES or status == "running":
        raise WorkflowError(f"실행 종료 상태 오류: {status!r}")
    with connect(path) as conn:
        r = _one(conn, "SELECT * FROM AGENT_RUN WHERE RUN_ID=?", (run_id,))
        if r is None:
            raise WorkflowError(f"존재하지 않는 실행: {run_id}")
        if r["STATUS"] != "running":
            raise WorkflowError(f"실행 {run_id} 는 이미 '{r['STATUS']}' 로 종료됨")
        started = datetime.strptime(r["STARTED_AT"], settings.DATETIME_FMT).replace(tzinfo=settings.KST)
        if duration_ms is None:
            duration_ms = int((settings.now_kst() - started).total_seconds() * 1000)
        conn.execute(
            """UPDATE AGENT_RUN SET STATUS=?, REPORT_JSON=?, TOOL_LOG_JSON=?, DETECTED_JSON=?, NUM_TURNS=?, COST_USD=?,
               DURATION_MS=?, ERROR_MESSAGE=?, SESSION_ID=?, FINISHED_AT=?,
               MODEL=COALESCE(?, MODEL), QUESTION=COALESCE(?, QUESTION), SCOPE_CODES=COALESCE(?, SCOPE_CODES)
               WHERE RUN_ID=?""",
            (status,
             json.dumps(report, ensure_ascii=False) if report is not None else None,
             json.dumps(tool_log, ensure_ascii=False) if tool_log is not None else None,
             json.dumps(detected, ensure_ascii=False) if detected is not None else None,
             num_turns, cost_usd, duration_ms, error_message, session_id, ts(),
             model, question,
             json.dumps(list(scope_codes), ensure_ascii=False) if scope_codes is not None else None,
             run_id),
        )


def add_alert(run_id: int, center_code: str, center_status: str, reasons: list[str], fingerprint: str,
              targeted: bool = False, skip_reason: str | None = None, path: str | None = None) -> int:
    """MON_ALERT 탐지 기록 1건. 반환: ALERT_ID."""
    code = normalize_code("CENTER", center_code)
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO MON_ALERT (RUN_ID, CENTER_CODE, CENTER_STATUS, REASONS_JSON, FINGERPRINT, TARGETED, SKIP_REASON, DETECTED_AT) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, code, center_status, json.dumps(list(reasons), ensure_ascii=False), fingerprint, 1 if targeted else 0, skip_reason, ts()),
        )
        return int(cur.lastrowid)
