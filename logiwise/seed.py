"""샘플 데이터 생성.

- 7개 센터, 거래처 5곳, 상품 20개, 실행일 기준 최근 7일 KPI·입출고, 미처리 이벤트, 본사 지시 2건.
- 데모 조건: C003 = 위험(정시출고율 < 95%, 미처리 상태이상 ≥ 4건),
  C005 = 주의(정시출고율 97% 미달 + 미납 이벤트), C006 = 주의(미입고 이벤트 → 입고 병목).
  나머지 4곳은 정상(목표 달성, 미처리 이벤트 없음).
- 이미 데이터가 있으면 건너뛴다. ``--force`` 일 때만 지우고 다시 만든다.
- 난수는 고정 시드를 써서 재생성해도 같은 모양이 나온다.

실행: ``python -m logiwise.seed [--force] [--db 경로]``
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import timedelta

from . import db, settings
from .settings import normalize_code, ts

SEED = 20261004

# ------------------------------------------------------------------ 마스터 정의
CENTERS = [
    ("C001", "서울센터", "수도권", "김서울", "02-1000-0001"),
    ("C002", "인천센터", "수도권", "박인천", "032-1000-0002"),
    ("C003", "대전센터", "충청권", "이대전", "042-1000-0003"),
    ("C004", "광주센터", "호남권", "최광주", "062-1000-0004"),
    ("C005", "대구센터", "영남권", "정대구", "053-1000-0005"),
    ("C006", "부산센터", "영남권", "강부산", "051-1000-0006"),
    ("C007", "강릉센터", "강원권", "윤강릉", "033-1000-0007"),
]

VENDORS = [
    ("V00001", "한빛식품", "제조사"),
    ("V00002", "대한생활", "제조사"),
    ("V00003", "코리아유통", "유통사"),
    ("V00004", "청정물산", "제조사"),
    ("V00005", "서진상사", "유통사"),
]

# (상품명, 거래처, 카테고리, 단가)
PRODUCTS = [
    ("생수 2L 6입", "V00001", "음료", 4800), ("라면 5입", "V00001", "식품", 4200),
    ("즉석밥 12입", "V00001", "식품", 11900), ("참치캔 3입", "V00001", "식품", 6900),
    ("주방세제 1L", "V00002", "생활", 5500), ("화장지 30롤", "V00002", "생활", 15900),
    ("물티슈 10입", "V00002", "생활", 8900), ("세탁세제 3L", "V00002", "생활", 12900),
    ("A4 복사지 2500매", "V00003", "사무", 22000), ("볼펜 12입", "V00003", "사무", 3600),
    ("포스트잇 세트", "V00003", "사무", 4900), ("박스테이프 6입", "V00003", "사무", 7200),
    ("쌀 10kg", "V00004", "식품", 32000), ("현미 5kg", "V00004", "식품", 19800),
    ("식용유 1.8L", "V00004", "식품", 7900), ("간장 1.7L", "V00004", "식품", 6400),
    ("종이컵 1000입", "V00005", "소모품", 9800), ("비닐봉투 500입", "V00005", "소모품", 5600),
    ("고무장갑 10켤레", "V00005", "소모품", 8300), ("마스크 50입", "V00005", "소모품", 9900),
]

ETC_CODES = {
    "EVENT_TYPE": ["미납", "미입고", "상태이상"],
    "ANOMALY_TYPE": ["파손", "유통기한임박", "라벨오류", "수량불일치", "온도이탈"],
    "ACTION_TYPE": ["재출고", "대체출고", "입고확인", "거래처독촉", "재검수", "폐기", "보류"],
    "WF_STATUS": ["지시완료", "센터확인중", "조치중", "완료"],
    "PRIORITY": ["보통", "높음", "긴급"],
    "DRAFT_STATUS": ["대기", "승인", "반려"],
    "CENTER_STATUS": ["정상", "주의", "위험", "데이터 없음"],
    "STAGE": ["입고", "보관", "피킹", "패킹", "출고"],
}

# 센터별 정시출고율 궤적(7일, 오늘이 마지막). 마지막 날이 등급을 결정한다.
ON_TIME_PROFILE = {
    "C001": [98.2, 98.6, 98.1, 98.9, 98.4, 98.7, 98.5],
    "C002": [97.6, 97.9, 98.0, 97.5, 97.8, 97.7, 97.9],
    "C003": [96.4, 95.9, 95.2, 94.6, 93.8, 93.1, 92.4],   # 위험: < 95
    "C004": [98.0, 97.9, 98.3, 98.1, 97.8, 98.2, 98.1],
    "C005": [97.3, 97.1, 96.8, 96.5, 96.9, 96.4, 96.2],   # 주의: 95 ≤ x < 97
    "C006": [97.8, 97.5, 97.9, 97.4, 97.7, 97.3, 97.6],   # 주의: 미입고 이벤트 때문
    "C007": [97.4, 97.6, 97.2, 97.8, 97.5, 97.3, 97.4],
}

# 미처리 이벤트 정의: (센터, 유형, 이상유형, 수량, 기처리수량, 발생 N시간 전, 기한 = 발생 + M시간, 메모)
OPEN_EVENTS = [
    # C003 위험: 상태이상 5건(미처리), 미납 2건(합 7 > 5 → 출고 병목), 미입고 1건
    ("C003", "상태이상", "파손", 3, 0, 52, 24, "입고 검수 중 박스 파손 확인"),
    ("C003", "상태이상", "온도이탈", 2, 0, 40, 24, "냉장 구역 온도 경보"),
    ("C003", "상태이상", "라벨오류", 4, 1, 30, 24, "바코드 미인식"),
    ("C003", "상태이상", "수량불일치", 1, 0, 20, 48, "실사 수량 차이"),
    ("C003", "상태이상", "유통기한임박", 6, 0, 10, 48, "유통기한 7일 이내"),
    ("C003", "미납", None, 4, 0, 36, 24, "출고 지연으로 미납 발생"),
    ("C003", "미납", None, 3, 0, 14, 24, "피킹 누락"),
    ("C003", "미입고", None, 2, 0, 8, 48, "거래처 납기 지연"),
    # C005 주의: 미납 2건(합 5, 병목 기준 5 초과 아님)
    ("C005", "미납", None, 3, 1, 26, 24, "대체 상품 협의 중"),
    ("C005", "미납", None, 2, 0, 6, 48, "출고 차량 지연"),
    # C006 주의: 미입고 2건(합 5 > 3 → 입고 병목), 상태이상 1건(보관 병목)
    ("C006", "미입고", None, 3, 0, 30, 24, "거래처 출하 지연"),
    ("C006", "미입고", None, 2, 0, 12, 48, "운송 중"),
    ("C006", "상태이상", "파손", 1, 0, 5, 48, "보관 중 낙하 파손"),
]

# 처리완료 이벤트(이력용): (센터, 유형, 이상유형, 수량, 발생 N시간 전, 조치유형)
CLOSED_EVENTS = [
    ("C001", "미납", None, 2, 70, "재출고"),
    ("C002", "상태이상", "라벨오류", 1, 60, "재검수"),
    ("C004", "미입고", None, 3, 90, "입고확인"),
    ("C007", "미납", None, 1, 48, "대체출고"),
    ("C003", "미납", None, 2, 100, "재출고"),
    ("C005", "상태이상", "파손", 1, 80, "폐기"),
]


# ------------------------------------------------------------------ 공개 API
def has_data(path: str | None = None) -> bool:
    """센터 마스터에 행이 있으면 True."""
    with db.connect(path) as conn:
        try:
            n = conn.execute("SELECT COUNT(*) FROM MT_CENTER").fetchone()[0]
        except sqlite3.OperationalError:
            return False
    return n > 0


def reset(path: str | None = None) -> None:
    """모든 테이블의 데이터를 지운다(스키마는 유지). 순환 참조가 있어 외래키를 잠시 끈다."""
    db.init_schema(path)
    with db.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for t in db.TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.execute("PRAGMA foreign_keys = ON")


def seed(path: str | None = None, force: bool = False) -> bool:
    """샘플 데이터를 넣는다. 이미 있으면 건너뛰고 False, 넣었으면 True.

    ``force=True`` 면 기존 데이터를 지우고 다시 만든다.
    """
    db.init_schema(path)
    if has_data(path):
        if not force:
            return False
        reset(path)

    rng = random.Random(SEED)
    now = settings.now_kst()
    days = [(now - timedelta(days=6 - i)).strftime(settings.DATE_FMT) for i in range(7)]

    with db.connect(path) as conn:
        _seed_master(conn, now)
        stock_by_center = _seed_stock(conn, rng, now)
        _seed_inout(conn, rng, days, now)
        open_summary = _seed_events(conn, now)
        _seed_kpi(conn, rng, days, stock_by_center, open_summary)

    # 본사 지시 2건 (db 함수로 생성 → 상태 전환 규칙 검증)
    i1 = db.send_instruction("C003", "상태이상 5건 긴급 점검 및 조치 보고",
                             "대전센터 미처리 상태이상 5건(파손·온도이탈·라벨오류 등)을 금일 중 재검수하고 "
                             "폐기/재검수 결과를 보고 바랍니다. 정시출고율 하락 원인도 함께 확인 요청.",
                             priority="긴급", path=path)
    i2 = db.send_instruction("C005", "미납 2건 재출고 처리",
                             "대구센터 미납 2건을 대체출고 또는 재출고로 처리하고 결과를 보고 바랍니다.",
                             priority="높음", path=path)
    db.acknowledge_instruction(i2, path=path)   # C005 는 센터확인중 상태로 둔다
    _ = i1
    return True


# ------------------------------------------------------------------ 내부
def _seed_master(conn: sqlite3.Connection, now) -> None:
    created = ts(now - timedelta(days=30))
    conn.executemany(
        "INSERT INTO MT_CENTER (CENTER_CODE, CENTER_NAME, REGION, MANAGER_NAME, PHONE, CREATED_AT) VALUES (?,?,?,?,?,?)",
        [(normalize_code("CENTER", c), n, r, m, p, created) for c, n, r, m, p in CENTERS],
    )
    conn.executemany(
        "INSERT INTO MT_VENDOR (VENDOR_CODE, VENDOR_NAME, VENDOR_TYPE, CREATED_AT) VALUES (?,?,?,?)",
        [(normalize_code("VENDOR", v), n, t, created) for v, n, t in VENDORS],
    )
    conn.executemany(
        "INSERT INTO MT_PRODUCT (PRODUCT_CODE, PRODUCT_NAME, VENDOR_CODE, CATEGORY, UNIT, UNIT_PRICE, CREATED_AT) VALUES (?,?,?,?,?,?,?)",
        [(normalize_code("PRODUCT", i + 1), n, normalize_code("VENDOR", v), cat, "EA", price, created)
         for i, (n, v, cat, price) in enumerate(PRODUCTS)],
    )
    rows = []
    for group, codes in ETC_CODES.items():
        for order, code in enumerate(codes, start=1):
            rows.append((group, code, code, order, "Y"))
    conn.executemany(
        "INSERT INTO MT_ETC_CODE (CODE_GROUP, CODE, CODE_NAME, SORT_ORDER, USE_YN) VALUES (?,?,?,?,?)", rows
    )


def _seed_stock(conn: sqlite3.Connection, rng: random.Random, now) -> dict[str, tuple[int, int]]:
    """센터별 재고. 반환: {센터: (총재고, SKU수)}."""
    updated = ts(now)
    result: dict[str, tuple[int, int]] = {}
    product_codes = [normalize_code("PRODUCT", i + 1) for i in range(len(PRODUCTS))]
    for code, *_ in CENTERS:
        k = rng.randint(14, 20)
        picked = sorted(rng.sample(product_codes, k))
        rows = [(code, p, rng.randint(40, 600), updated) for p in picked]
        conn.executemany(
            "INSERT INTO INV_CENTER_STOCK (CENTER_CODE, PRODUCT_CODE, STOCK_QTY, UPDATED_AT) VALUES (?,?,?,?)", rows
        )
        result[code] = (sum(r[2] for r in rows), len(rows))
    return result


def _seed_inout(conn: sqlite3.Connection, rng: random.Random, days: list[str], now) -> None:
    """일별 입출고 집계 7일 + 최근 2일 단계별 이력 일부."""
    base = {"C001": 420, "C002": 360, "C003": 300, "C004": 330, "C005": 310, "C006": 340, "C007": 220}
    product_codes = [normalize_code("PRODUCT", i + 1) for i in range(len(PRODUCTS))]
    for code, *_ in CENTERS:
        for i, d in enumerate(days):
            b = base[code]
            in_qty = int(b * rng.uniform(0.85, 1.15))
            # C003 은 출고가 점점 줄고, C006 은 입고가 줄어드는 모양
            out_factor = 1.0 - (0.04 * i if code == "C003" else 0)
            in_factor = 1.0 - (0.05 * i if code == "C006" else 0)
            in_qty = int(in_qty * in_factor)
            out_qty = int(b * rng.uniform(0.8, 1.1) * out_factor)
            storage = int(in_qty * rng.uniform(0.9, 1.0))
            picking = int(out_qty * rng.uniform(1.0, 1.1))
            packing = int(out_qty * rng.uniform(0.95, 1.05))
            conn.execute(
                "INSERT INTO INV_CENTER_INOUT_DAILY (CENTER_CODE, INOUT_DATE, IN_QTY, STORAGE_QTY, PICKING_QTY, PACKING_QTY, OUT_QTY) VALUES (?,?,?,?,?,?,?)",
                (code, d, in_qty, storage, picking, packing, out_qty),
            )
        # 단계별 이력: 최근 2일, 센터당 10건
        for _ in range(10):
            stage = rng.choice(ETC_CODES["STAGE"])
            occurred = now - timedelta(hours=rng.randint(1, 47))
            conn.execute(
                "INSERT INTO INV_CENTER_INOUT_HISTORY (CENTER_CODE, PRODUCT_CODE, STAGE, QTY, OCCURRED_AT) VALUES (?,?,?,?,?)",
                (code, rng.choice(product_codes), stage, rng.randint(5, 80), ts(occurred)),
            )


def _seed_events(conn: sqlite3.Connection, now) -> dict[str, dict[str, int]]:
    """이벤트 삽입. 반환: 센터별 미처리 집계 {센터: {shortage, unreceived, anomaly}} (KPI 마지막 날과 맞추기 위함)."""
    summary: dict[str, dict[str, int]] = {c: {"shortage": 0, "unreceived": 0, "anomaly": 0} for c, *_ in CENTERS}
    product_cycle = [normalize_code("PRODUCT", i + 1) for i in range(len(PRODUCTS))]
    pi = 0
    for center, etype, atype, qty, resolved, hours_ago, due_hours, note in OPEN_EVENTS:
        occurred = now - timedelta(hours=hours_ago)
        due = occurred + timedelta(hours=due_hours)
        status = "부분처리" if resolved > 0 else "미처리"
        cur = conn.execute(
            """INSERT INTO EXC_CENTER_EVENT (CENTER_CODE, PRODUCT_CODE, EVENT_TYPE, ANOMALY_TYPE, EVENT_QTY, RESOLVED_QTY, STATUS, OCCURRED_AT, DUE_AT, NOTE)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (center, product_cycle[pi % len(product_cycle)], etype, atype, qty, resolved, status, ts(occurred), ts(due), note),
        )
        pi += 1
        if resolved > 0:
            conn.execute(
                "INSERT INTO EXC_CENTER_EVENT_RESOLVE (EVENT_ID, ACTION_TYPE, ACTION_QTY, ACTION_NOTE, RESOLVED_BY, RESOLVED_AT) VALUES (?,?,?,?,?,?)",
                (cur.lastrowid, "재출고" if etype == "미납" else "재검수", resolved, "일부 조치", "센터", ts(occurred + timedelta(hours=3))),
            )
        key = {"미납": "shortage", "미입고": "unreceived", "상태이상": "anomaly"}[etype]
        summary[center][key] += (qty - resolved) if key != "anomaly" else 1

    for center, etype, atype, qty, hours_ago, action in CLOSED_EVENTS:
        occurred = now - timedelta(hours=hours_ago)
        resolved_at = occurred + timedelta(hours=6)
        cur = conn.execute(
            """INSERT INTO EXC_CENTER_EVENT (CENTER_CODE, PRODUCT_CODE, EVENT_TYPE, ANOMALY_TYPE, EVENT_QTY, RESOLVED_QTY, STATUS, OCCURRED_AT, DUE_AT, RESOLVED_AT, NOTE)
               VALUES (?,?,?,?,?,?,'처리완료',?,?,?,?)""",
            (center, product_cycle[pi % len(product_cycle)], etype, atype, qty, qty, ts(occurred), ts(occurred + timedelta(hours=24)), ts(resolved_at), "처리 완료"),
        )
        pi += 1
        conn.execute(
            "INSERT INTO EXC_CENTER_EVENT_RESOLVE (EVENT_ID, ACTION_TYPE, ACTION_QTY, ACTION_NOTE, RESOLVED_BY, RESOLVED_AT) VALUES (?,?,?,?,?,?)",
            (cur.lastrowid, action, qty, "조치 완료", "센터", ts(resolved_at)),
        )
    return summary


def _seed_kpi(conn: sqlite3.Connection, rng: random.Random, days: list[str],
              stock_by_center: dict[str, tuple[int, int]], open_summary: dict[str, dict[str, int]]) -> None:
    """7일 KPI. 마지막 날의 미납/미입고/이상건수는 미처리 이벤트 집계와 같게 둔다."""
    for code, *_ in CENTERS:
        total, skus = stock_by_center[code]
        profile = ON_TIME_PROFILE[code]
        for i, d in enumerate(days):
            last = i == len(days) - 1
            if last:
                s = open_summary[code]
                shortage, unreceived, anomaly = s["shortage"], s["unreceived"], s["anomaly"]
            else:
                # 과거일은 작은 난수. C003 은 점점 나빠지는 모양
                worsen = i if code == "C003" else 0
                shortage = rng.randint(0, 2) + (worsen // 2)
                unreceived = rng.randint(0, 2)
                anomaly = rng.randint(0, 1) + (worsen // 2)
            stock = int(total * rng.uniform(0.9, 1.0)) if not last else total
            conn.execute(
                """INSERT INTO AN_CENTER_KPI_DAILY (CENTER_CODE, KPI_DATE, ON_TIME_RATE, STOCK_QTY, SKU_COUNT, OUTBOUND_WIP, UNRECEIVED_QTY, SHORTAGE_QTY, ANOMALY_COUNT)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (code, d, profile[i], stock, skus, rng.randint(5, 30), unreceived, shortage, anomaly),
            )


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LOGIWISE 샘플 데이터 생성")
    parser.add_argument("--force", action="store_true", help="기존 데이터를 지우고 다시 만든다")
    parser.add_argument("--db", default=None, help="DB 경로(기본: LOGIWISE_DB_PATH 또는 config.toml)")
    args = parser.parse_args(argv)
    path = args.db or settings.get_db_path()
    created = seed(path=path, force=args.force)
    if created:
        print(f"[seed] 샘플 데이터 생성 완료: {path}")
    else:
        print(f"[seed] 이미 데이터가 있어 건너뜀 (--force 로 재생성): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
