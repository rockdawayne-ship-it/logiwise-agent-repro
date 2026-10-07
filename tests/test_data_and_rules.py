"""데이터 계층 테스트: 테이블 존재, seed 멱등성, 코드 검증, overview 집계 일치, 데모 조건."""

from __future__ import annotations

import sqlite3

import pytest

from logiwise import db, seed, settings
from logiwise.settings import CodeError, normalize_code

EXPECTED_TABLES = {
    "MT_CENTER", "MT_VENDOR", "MT_PRODUCT", "MT_ETC_CODE",
    "INV_CENTER_STOCK", "INV_CENTER_INOUT_HISTORY", "INV_CENTER_INOUT_DAILY",
    "AN_CENTER_KPI_DAILY",
    "EXC_CENTER_EVENT", "EXC_CENTER_EVENT_RESOLVE",
    "WF_INSTRUCTION", "WF_ACTION_REPORT",
    "AGENT_RUN", "MON_ALERT", "WF_DRAFT",
}


def _count(path: str, table: str) -> int:
    with db.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------------------ 스키마
def test_tables_exist(db_path):
    names = set(db.table_names(db_path))
    assert EXPECTED_TABLES <= names
    assert len(EXPECTED_TABLES) == 15


def test_conftest_isolated_db():
    """settings 가 임시 경로를 보고 있어야 한다(실제 data/ 를 건드리지 않음)."""
    assert "logiwise_test_" in settings.get_db_path()
    assert "logiwise_test_" in settings.DB_PATH


def test_foreign_keys_enforced(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO EXC_CENTER_EVENT (CENTER_CODE, EVENT_TYPE, EVENT_QTY, STATUS, OCCURRED_AT, DUE_AT) VALUES ('C999','미납',1,'미처리','2026-01-01 00:00:00','2026-01-02 00:00:00')"
            )


# ------------------------------------------------------------------ seed
def test_seed_counts(db_path):
    assert _count(db_path, "MT_CENTER") == 7
    assert _count(db_path, "MT_PRODUCT") == 20
    assert _count(db_path, "AN_CENTER_KPI_DAILY") == 7 * 7
    assert _count(db_path, "INV_CENTER_INOUT_DAILY") == 7 * 7
    assert _count(db_path, "WF_INSTRUCTION") == 2
    assert len(db.events(only_open=True, path=db_path)) > 0


def test_seed_is_idempotent(db_path):
    before = {t: _count(db_path, t) for t in db.TABLES}
    assert seed.seed(path=db_path) is False          # 이미 있으면 건너뜀
    after = {t: _count(db_path, t) for t in db.TABLES}
    assert before == after


def test_seed_force_regenerates(db_path):
    # 사용자 데이터를 하나 추가한 뒤 --force 로 재생성하면 원래 모양으로 돌아온다
    db.send_instruction("C001", "임시 지시", "본문", path=db_path)
    assert _count(db_path, "WF_INSTRUCTION") == 3
    assert seed.seed(path=db_path, force=True) is True
    assert _count(db_path, "WF_INSTRUCTION") == 2
    assert _count(db_path, "MT_CENTER") == 7


def test_seed_cli_skips_without_force(db_path, capsys):
    assert seed.main(["--db", db_path]) == 0
    assert "건너뜀" in capsys.readouterr().out


# ------------------------------------------------------------------ 코드 검증
@pytest.mark.parametrize("kind,value,expected", [
    ("CENTER", "C001", "C001"),
    ("CENTER", "c3", "C003"),
    ("CENTER", 7, "C007"),
    ("VENDOR", "V00001", "V00001"),
    ("VENDOR", 1, "V00001"),
    ("PRODUCT", "1", "0000000001"),
    ("PRODUCT", 20, "0000000020"),
    ("product", " 0000000005 ", "0000000005"),
])
def test_normalize_code_ok(kind, value, expected):
    assert normalize_code(kind, value) == expected


@pytest.mark.parametrize("kind,value", [
    ("CENTER", "C12345"),       # 자릿수 초과
    ("CENTER", "ABCD"),         # 숫자 아님
    ("CENTER", ""),
    ("CENTER", None),
    ("VENDOR", "C00001"),       # 접두 문자 다름
    ("PRODUCT", "12345678901"), # 11자리
    ("PRODUCT", "P000000001"),
    ("UNKNOWN", "1"),
])
def test_normalize_code_rejects(kind, value):
    with pytest.raises(CodeError):
        normalize_code(kind, value)


def test_seeded_codes_have_correct_width(db_path):
    with db.connect(db_path) as conn:
        for (c,) in conn.execute("SELECT CENTER_CODE FROM MT_CENTER"):
            assert normalize_code("CENTER", c) == c and len(c) == 4
        for (v,) in conn.execute("SELECT VENDOR_CODE FROM MT_VENDOR"):
            assert normalize_code("VENDOR", v) == v and len(v) == 6
        for (p,) in conn.execute("SELECT PRODUCT_CODE FROM MT_PRODUCT"):
            assert normalize_code("PRODUCT", p) == p and len(p) == 10


# ------------------------------------------------------------------ 조회
def test_overview_matches_open_events(db_path):
    ov = db.overview(path=db_path)
    assert len(ov) == 7
    open_events = db.events(only_open=True, path=db_path)
    assert sum(r["open_event_count"] for r in ov) == len(open_events)
    for r in ov:
        mine = [e for e in open_events if e["CENTER_CODE"] == r["center_code"]]
        assert r["open_event_count"] == len(mine)
        assert r["open_anomaly_count"] == sum(1 for e in mine if e["EVENT_TYPE"] == "상태이상")
        assert r["open_shortage_qty"] == sum(e["REMAIN_QTY"] for e in mine if e["EVENT_TYPE"] == "미납")
        # 최신 KPI 의 미납/미입고/이상건수는 미처리 이벤트 집계와 같아야 한다
        assert r["anomaly_count"] == r["open_anomaly_count"]
        assert r["shortage_qty"] == r["open_shortage_qty"]
        assert r["unreceived_qty"] == r["open_unreceived_qty"]


def test_overview_open_instruction_count(db_path):
    ov = {r["center_code"]: r for r in db.overview(path=db_path)}
    assert ov["C003"]["open_instruction_count"] == 1
    assert ov["C005"]["open_instruction_count"] == 1
    assert ov["C001"]["open_instruction_count"] == 0


def test_kpi_trend_and_inout_daily(db_path):
    trend = db.kpi_trend(path=db_path)
    assert len(trend) == 49
    c3 = db.kpi_trend("C003", path=db_path)
    assert len(c3) == 7 and c3[0]["KPI_DATE"] < c3[-1]["KPI_DATE"]
    assert c3[-1]["KPI_DATE"] == settings.today_kst()
    io = db.inout_daily("C006", path=db_path)
    assert len(io) == 7 and all(k in io[0] for k in ("IN_QTY", "PICKING_QTY", "OUT_QTY"))


def test_demo_conditions_match_rules_thresholds(db_path):
    """C003 위험, C005·C006 주의, 나머지 정상이 되도록 seed 가 config 임계값을 만족하는지."""
    R = settings.RULES
    ov = {r["center_code"]: r for r in db.overview(path=db_path)}
    # 위험 = 정시출고율 < danger_on_time 또는 미처리 상태이상 ≥ danger_anomaly_count
    assert ov["C003"]["on_time_rate"] < R["danger_on_time"]
    assert ov["C003"]["open_anomaly_count"] >= R["danger_anomaly_count"]
    assert ov["C003"]["shortage_qty"] > R["shortage_bottleneck"]      # 출고 병목
    # 주의 = 목표 미달 또는 미처리 이벤트 존재 (위험은 아님)
    for code in ("C005", "C006"):
        r = ov[code]
        assert r["on_time_rate"] >= R["danger_on_time"] and r["open_anomaly_count"] < R["danger_anomaly_count"]
        assert r["on_time_rate"] < R["on_time_target"] or r["open_event_count"] > 0
    assert ov["C005"]["on_time_rate"] < R["on_time_target"]
    assert ov["C006"]["unreceived_qty"] > R["unreceived_bottleneck"]  # 입고 병목
    # 정상
    for code in ("C001", "C002", "C004", "C007"):
        assert ov[code]["on_time_rate"] >= R["on_time_target"]
        assert ov[code]["open_event_count"] == 0


def test_config_rules_values():
    R = settings.RULES
    assert R["on_time_target"] == 97.0
    assert R["unreceived_bottleneck"] == 3 and R["shortage_bottleneck"] == 5
    assert R["heatmap_yellow_min"] == 1 and R["heatmap_red_min"] == 4
    assert R["danger_on_time"] == 95.0 and R["danger_anomaly_count"] == 4
    for section in ("database", "agent", "monitor", "rules"):
        assert section in settings.CONFIG
    assert settings.AGENT["model"]
    assert settings.MONITOR["max_centers_per_cycle"] >= 1


# ================================================================== 규칙 계층 (Step 2)
from datetime import datetime, timedelta  # noqa: E402

from logiwise import rules  # noqa: E402
from logiwise.models import AgentReport  # noqa: E402


def _row(on_time_rate=98.0, anomaly=0, open_events=0, unreceived=0, shortage=0, overdue=0,
         open_instr=0, kpi_date="2026-10-07", code="C001"):
    """overview 행과 같은 모양의 합성 행. 미처리 상태이상(open_anomaly_count)과 KPI 이상건수를 같게 둔다."""
    return {
        "center_code": code, "center_name": "테스트센터", "kpi_date": kpi_date, "on_time_rate": on_time_rate,
        "unreceived_qty": unreceived, "shortage_qty": shortage, "anomaly_count": anomaly,
        "open_event_count": open_events, "open_anomaly_count": anomaly,
        "open_unreceived_qty": unreceived, "open_shortage_qty": shortage,
        "overdue_event_count": overdue, "open_instruction_count": open_instr,
    }


# ------------------------------------------------------------------ 등급 경계값
@pytest.mark.parametrize("rate,anomaly,open_events,expected", [
    (94.9, 0, 0, "위험"),     # 정시출고율 < 95
    (95.0, 0, 0, "주의"),     # 95 이상이지만 목표 97 미달
    (96.9, 0, 0, "주의"),
    (97.0, 0, 0, "정상"),     # 목표 달성, 미처리 없음
    (98.5, 0, 0, "정상"),
    (97.0, 0, 1, "주의"),     # 목표 달성이지만 미처리 이벤트 존재
    (97.0, 3, 3, "주의"),     # 상태이상 3건은 주의
    (97.0, 4, 4, "위험"),     # 상태이상 ≥ 4건은 위험
    (99.0, 10, 10, "위험"),
])
def test_center_status_boundaries(rate, anomaly, open_events, expected):
    assert rules.center_status(_row(on_time_rate=rate, anomaly=anomaly, open_events=open_events)) == expected


def test_center_status_no_data():
    assert rules.center_status({"center_code": "C009", "kpi_date": None, "on_time_rate": None}) == "데이터 없음"
    assert rules.reasons_for({"center_code": "C009", "kpi_date": None, "on_time_rate": None}) == ["기준일 KPI 없음(데이터 없음)"]


def test_center_status_uses_config_thresholds(monkeypatch):
    """임계값은 하드코딩이 아니라 settings.RULES 를 읽어야 한다."""
    monkeypatch.setitem(settings.RULES, "danger_on_time", 90.0)
    assert rules.center_status(_row(on_time_rate=92.0)) == "주의"
    monkeypatch.setitem(settings.RULES, "on_time_target", 91.0)
    assert rules.center_status(_row(on_time_rate=92.0)) == "정상"


# ------------------------------------------------------------------ 병목 (PRD 4.2.3)
@pytest.mark.parametrize("unreceived,anomaly,shortage,expected", [
    (0, 0, 0, []),
    (3, 0, 0, []),              # 미입고 3 은 병목 아님(> 3 이어야)
    (4, 0, 0, ["입고"]),
    (0, 1, 0, ["보관"]),        # 이상건수 > 0
    (0, 0, 5, []),              # 미납 5 는 병목 아님(> 5 이어야)
    (0, 0, 6, ["출고"]),
    (4, 1, 6, ["입고", "보관", "출고"]),
])
def test_bottlenecks(unreceived, anomaly, shortage, expected):
    row = _row(unreceived=unreceived, anomaly=anomaly, shortage=shortage)
    assert rules.bottlenecks(row) == expected
    flags = rules.bottleneck_flags(row)
    assert set(flags) == set(rules.STAGES)
    assert flags["피킹"] is False and flags["패킹"] is False     # PRD 에 기준 없음 → 항상 False
    assert [s for s in rules.STAGES if flags[s]] == expected


def test_picking_packing_never_bottleneck_even_with_extreme_values():
    row = _row(unreceived=999, anomaly=999, shortage=999)
    row["picking_qty"] = 0
    row["packing_qty"] = 0
    flags = rules.bottleneck_flags(row)
    assert flags["피킹"] is False and flags["패킹"] is False
    assert rules.bottlenecks(row) == ["입고", "보관", "출고"]


# ------------------------------------------------------------------ 히트맵 (PRD 4.1.3)
@pytest.mark.parametrize("count,expected", [
    (0, "녹"), (1, "노"), (2, "노"), (3, "노"), (4, "적"), (10, "적"), (None, "녹"), (-1, "녹"),
])
def test_heatmap_level(count, expected):
    assert rules.heatmap_level(count) == expected


# ------------------------------------------------------------------ 이벤트 지연색 (보완)
def test_event_delay_level_boundaries():
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=settings.KST)
    fmt = lambda dt: dt.strftime(settings.DATETIME_FMT)  # noqa: E731
    assert rules.event_delay_level(fmt(now + timedelta(hours=1)), now) == "녹"      # 기한 전
    assert rules.event_delay_level(fmt(now), now) == "녹"                           # 정확히 기한
    assert rules.event_delay_level(fmt(now - timedelta(hours=1)), now) == "노"      # 1h 지연
    assert rules.event_delay_level(fmt(now - timedelta(hours=23, minutes=59)), now) == "노"
    assert rules.event_delay_level(fmt(now - timedelta(hours=24)), now) == "적"     # 정확히 24h
    assert rules.event_delay_level(fmt(now - timedelta(hours=25)), now) == "적"
    # datetime 입력, now 생략(현재 기준)도 동작
    assert rules.event_delay_level(settings.now_kst() + timedelta(days=1)) == "녹"
    assert rules.event_delay_level(settings.now_kst() - timedelta(days=3)) == "적"
    assert rules.event_delay_hours(fmt(now - timedelta(hours=2)), now) == pytest.approx(2.0)
    assert rules.event_delay_hours(fmt(now + timedelta(hours=2)), now) == 0.0


def test_seeded_events_have_mixed_delay_levels(db_path):
    levels = {rules.event_delay_level(e["DUE_AT"]) for e in db.events(only_open=True, path=db_path)}
    assert {"녹", "적"} <= levels       # seed 에는 기한 전 이벤트와 24h 이상 지연 이벤트가 모두 있다


# ------------------------------------------------------------------ 사유
def test_reasons_for_synthetic_row():
    row = _row(on_time_rate=92.1, anomaly=0, open_events=5, shortage=8, overdue=2, open_instr=1)
    reasons = rules.reasons_for(row)
    assert reasons == [
        "정시출고율 92.1% < 위험 기준 95%",
        "출고 병목(미납 8)",
        "미처리 이벤트 5건(기한초과 2건)",
        "미완료 지시 1건",
    ]
    assert rules.reasons_for(_row(on_time_rate=96.5)) == ["정시출고율 96.5% < 목표 97%"]
    assert rules.reasons_for(_row()) == []


def test_reasons_for_seeded_c003(db_path):
    ov = {r["center_code"]: r for r in db.overview(path=db_path)}
    reasons = rules.reasons_for(ov["C003"])
    joined = " / ".join(reasons)
    assert "정시출고율" in joined and "출고 병목(미납" in joined and "보관 병목(상태이상" in joined
    assert "미처리 이벤트" in joined and "기한초과" in joined and "미완료 지시 1건" in joined


# ------------------------------------------------------------------ 스냅샷
def test_snapshot_shape_and_demo_statuses(db_path):
    snap = rules.snapshot(path=db_path)
    assert set(snap) >= {"kpi_day", "data_kind", "rules", "note", "centers"}
    assert snap["kpi_day"] == settings.today_kst() and snap["data_kind"] == "sample"
    assert snap["rules"]["on_time_target"] == settings.RULES["on_time_target"]
    assert "지시문이 아님" in snap["note"]
    assert len(snap["centers"]) == 7
    status = {c["center_code"]: c["status"] for c in snap["centers"]}
    assert status["C003"] == "위험"
    assert status["C005"] == "주의" and status["C006"] == "주의"
    assert all(status[c] == "정상" for c in ("C001", "C002", "C004", "C007"))
    for c in snap["centers"]:
        assert c["evidence_id"] == f"KPI:{c['center_code']}:{c['kpi_date']}"
        assert isinstance(c["bottlenecks"], list) and isinstance(c["reasons"], list)
        assert (c["status"] == "정상") == (c["reasons"] == [])
    bn = {c["center_code"]: c["bottlenecks"] for c in snap["centers"]}
    assert "출고" in bn["C003"] and "보관" in bn["C003"]
    assert "입고" in bn["C006"]
    assert bn["C001"] == []


def test_snapshot_single_center_and_bad_code(db_path):
    snap = rules.snapshot("c3", path=db_path)
    assert [c["center_code"] for c in snap["centers"]] == ["C003"]
    with pytest.raises(CodeError):
        rules.snapshot("ABCD", path=db_path)


def test_snapshot_center_without_kpi_is_no_data(db_path):
    with db.connect(db_path) as conn:
        conn.execute("INSERT INTO MT_CENTER VALUES ('C008','신규센터','제주권',NULL,NULL,'2026-10-01 00:00:00')")
    snap = rules.snapshot("C008", path=db_path)
    c = snap["centers"][0]
    assert c["status"] == "데이터 없음" and c["bottlenecks"] == []
    assert c["evidence_id"] == f"KPI:C008:{snap['kpi_day']}"
    # 데이터 없음은 탐지 대상이 아니다
    assert "C008" not in [r["center_code"] for r in rules.detect_anomalies(path=db_path)]


# ------------------------------------------------------------------ 탐지 정렬
def test_detect_anomalies_order_and_fields(db_path):
    found = rules.detect_anomalies(path=db_path)
    assert [r["center_code"] for r in found] == ["C003", "C005", "C006"]   # 위험 먼저, 주의는 정시출고율 낮은 순
    assert [r["status"] for r in found] == ["위험", "주의", "주의"]
    assert found[1]["on_time_rate"] <= found[2]["on_time_rate"]
    open_events = db.events(only_open=True, path=db_path)
    for r in found:
        mine = sorted(e["EVENT_ID"] for e in open_events if e["CENTER_CODE"] == r["center_code"])
        assert r["open_event_ids"] == mine and len(mine) > 0
        assert len(r["fingerprint"]) == 16 and r["kpi_day"] == settings.today_kst()
        assert r["fingerprint"] == rules.fingerprint(r, r["open_event_ids"])


def test_detect_anomalies_sorts_danger_before_warn_regardless_of_rate(db_path):
    """주의 센터의 정시출고율이 위험 센터보다 낮아도 위험이 먼저 온다."""
    with db.connect(db_path) as conn:
        # C005(주의) 를 정시출고율 95.0 으로 낮추고, C003 은 상태이상으로만 위험이 되도록 97.5 로 올린다
        conn.execute("UPDATE AN_CENTER_KPI_DAILY SET ON_TIME_RATE=95.0 WHERE CENTER_CODE='C005' AND KPI_DATE=?", (settings.today_kst(),))
        conn.execute("UPDATE AN_CENTER_KPI_DAILY SET ON_TIME_RATE=97.5 WHERE CENTER_CODE='C003' AND KPI_DATE=?", (settings.today_kst(),))
    found = rules.detect_anomalies(path=db_path)
    assert found[0]["center_code"] == "C003" and found[0]["status"] == "위험"
    assert [r["center_code"] for r in found] == ["C003", "C005", "C006"]


# ------------------------------------------------------------------ 지문
def test_fingerprint_is_deterministic_and_ignores_continuous_values():
    row = _row(on_time_rate=92.4, anomaly=5, open_events=8, shortage=7, code="C003")
    fp1 = rules.fingerprint(row, [3, 1, 2])
    fp2 = rules.fingerprint(row, [1, 2, 3])          # 순서 무관
    assert fp1 == fp2 and len(fp1) == 16 and fp1.isalnum()
    row2 = dict(row, on_time_rate=93.9)              # 등급이 같으면 정시출고율이 흔들려도 같은 지문
    assert rules.fingerprint(row2, [1, 2, 3]) == fp1
    # 등급/병목/이벤트 집합이 바뀌면 지문이 바뀐다
    assert rules.fingerprint(dict(row, on_time_rate=96.0, anomaly=0, open_anomaly_count=0), [1, 2, 3]) != fp1
    assert rules.fingerprint(dict(row, shortage_qty=0, open_shortage_qty=0), [1, 2, 3]) != fp1   # 출고 병목 해제
    assert rules.fingerprint(row, [1, 2]) != fp1
    # 미리 계산된 status/bottlenecks 가 있으면 그것을 쓴다(plan 5.1 정의와 동일)
    import hashlib
    pre = {"center_code": "C003", "status": "위험", "bottlenecks": ["출고", "보관"]}
    key = "C003|위험|보관,출고|1,2,3"
    assert rules.fingerprint(pre, [1, 2, 3]) == hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def test_fingerprint_changes_after_event_resolved(db_path):
    before = {r["center_code"]: r for r in rules.detect_anomalies(path=db_path)}
    again = {r["center_code"]: r for r in rules.detect_anomalies(path=db_path)}
    assert before["C003"]["fingerprint"] == again["C003"]["fingerprint"]     # 아무 변화 없으면 그대로

    # C003 의 미납 이벤트 하나를 완전히 처리한다(등급은 여전히 위험)
    ev = next(e for e in db.events("C003", only_open=True, path=db_path) if e["EVENT_TYPE"] == "미납")
    db.resolve_event(ev["EVENT_ID"], "재출고", ev["REMAIN_QTY"], path=db_path)

    after = {r["center_code"]: r for r in rules.detect_anomalies(path=db_path)}
    assert after["C003"]["status"] == "위험"
    assert ev["EVENT_ID"] not in after["C003"]["open_event_ids"]
    assert after["C003"]["fingerprint"] != before["C003"]["fingerprint"]
    # 다른 센터의 지문은 영향받지 않는다
    assert after["C005"]["fingerprint"] == before["C005"]["fingerprint"]
    assert after["C006"]["fingerprint"] == before["C006"]["fingerprint"]


def test_fingerprint_changes_when_bottleneck_clears(db_path):
    """이벤트 ID 집합이 같아도 병목 집합이 바뀌면 지문이 바뀐다(등급 유지)."""
    before = {r["center_code"]: r for r in rules.detect_anomalies(path=db_path)}["C006"]
    assert "입고" in before["bottlenecks"]
    with db.connect(db_path) as conn:
        conn.execute("UPDATE AN_CENTER_KPI_DAILY SET UNRECEIVED_QTY=0 WHERE CENTER_CODE='C006' AND KPI_DATE=?", (settings.today_kst(),))
    after = {r["center_code"]: r for r in rules.detect_anomalies(path=db_path)}["C006"]
    assert after["status"] == before["status"] == "주의"
    assert after["open_event_ids"] == before["open_event_ids"]
    assert "입고" not in after["bottlenecks"]
    assert after["fingerprint"] != before["fingerprint"]


# ------------------------------------------------------------------ 규칙 기반 보고서
def test_rule_report_matches_agent_report_schema(db_path):
    rep = rules.rule_report(path=db_path)
    assert isinstance(rep, AgentReport)
    # 직렬화 → 재검증(구조화 출력과 같은 경로)
    AgentReport.model_validate(rep.model_dump())
    AgentReport.model_validate_json(rep.model_dump_json())
    first_line = rep.summary.splitlines()[0]
    assert settings.today_kst() in first_line and "샘플 데이터" in first_line and "규칙 기반" in first_line and "LLM 미사용" in first_line

    assert [f.center_code for f in rep.findings] == ["C003", "C005", "C006"]
    status = {c["center_code"]: c["status"] for c in rules.snapshot(path=db_path)["centers"]}
    snap_ids = {c["evidence_id"] for c in rules.snapshot(path=db_path)["centers"]}
    event_ids = {f"EVENT:{e['EVENT_ID']}" for e in db.events(only_open=True, path=db_path)}
    for f in rep.findings:
        assert f.severity == status[f.center_code]                      # 등급은 규칙 값 그대로
        assert f.hypothesis == rules.FIXED_HYPOTHESIS == "수치만으로 원인을 확정할 수 없음"
        assert f.evidence_ids[0] in snap_ids
        assert set(f.evidence_ids[1:]) <= event_ids and len(f.evidence_ids) >= 2
        assert f.observation and f.recommendation

    assert len(rep.instruction_drafts) == 3
    pri = {d.center_code: d.priority for d in rep.instruction_drafts}
    assert pri == {"C003": "높음", "C005": "보통", "C006": "보통"}
    for d in rep.instruction_drafts:
        assert 1 <= len(d.title) <= 120 and 1 <= len(d.body) <= 3000
        assert "규칙 기반" in d.body and "승인 전" in d.body
        assert set(d.evidence_ids) <= snap_ids | event_ids


def test_rule_report_with_targets_and_schema_limits(db_path):
    rep = rules.rule_report(["C005"], path=db_path)
    assert [f.center_code for f in rep.findings] == ["C005"] and len(rep.instruction_drafts) == 1
    rep2 = rules.rule_report(rules.detect_anomalies(path=db_path)[:2], path=db_path)
    assert [f.center_code for f in rep2.findings] == ["C003", "C005"]
    rep3 = rules.rule_report([], path=db_path)
    assert rep3.findings == [] and rep3.instruction_drafts == [] and "없음" in rep3.summary
    schema = AgentReport.model_json_schema()
    assert schema["properties"]["findings"]["maxItems"] == 7
    assert schema["properties"]["instruction_drafts"]["maxItems"] == 3
    assert schema["additionalProperties"] is False


def test_rules_module_has_no_llm_or_ui_imports():
    """결정론 계층은 LLM·Streamlit 을 import 하지 않는다."""
    src = (settings.PROJECT_ROOT / "logiwise" / "rules.py").read_text(encoding="utf-8")
    for banned in ("claude_agent_sdk", "anthropic", "streamlit", "from .agent", "import agent"):
        assert banned not in src
