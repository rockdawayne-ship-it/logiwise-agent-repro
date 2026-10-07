"""워크플로우 테스트: 지시 상태 전환, 이벤트 조치, 초안 승인/반려, 실행 기록."""

from __future__ import annotations

import pytest

from logiwise import db
from logiwise.db import WorkflowError


# ------------------------------------------------------------------ 지시 워크플로우
def test_full_instruction_flow(db_path):
    iid = db.send_instruction("C001", "테스트 지시", "본문입니다", priority="높음", path=db_path)
    row = db.get_instruction(iid, path=db_path)
    assert row["STATUS"] == "지시완료" and row["SOURCE"] == "본사직접" and row["PRIORITY"] == "높음"

    assert db.acknowledge_instruction(iid, path=db_path) == "센터확인중"
    assert db.get_instruction(iid, path=db_path)["ACKNOWLEDGED_AT"]

    rid = db.submit_report(iid, "재출고", "재출고 완료했습니다", path=db_path)
    row = db.get_instruction(iid, path=db_path)
    assert row["STATUS"] == "조치중" and row["REPORTED_AT"]
    reports = db.instructions("C001", path=db_path)[0]["REPORTS"]
    assert len(reports) == 1 and reports[0]["REPORT_ID"] == rid and reports[0]["APPROVED_AT"] is None

    assert db.approve_report(iid, path=db_path) == "완료"
    row = db.get_instruction(iid, path=db_path)
    assert row["STATUS"] == "완료" and row["APPROVED_AT"]
    assert db.instructions("C001", path=db_path)[0]["REPORTS"][0]["APPROVED_AT"]


@pytest.mark.parametrize("bad_step", ["submit", "approve"])
def test_reject_skipping_steps(db_path, bad_step):
    iid = db.send_instruction("C002", "지시", "본문", path=db_path)
    with pytest.raises(WorkflowError):
        if bad_step == "submit":
            db.submit_report(iid, "재출고", "보고", path=db_path)   # 확인 전 보고
        else:
            db.approve_report(iid, path=db_path)                     # 확인 전 승인
    assert db.get_instruction(iid, path=db_path)["STATUS"] == "지시완료"


def test_reject_repeated_and_backward_transitions(db_path):
    iid = db.send_instruction("C002", "지시", "본문", path=db_path)
    db.acknowledge_instruction(iid, path=db_path)
    with pytest.raises(WorkflowError):
        db.acknowledge_instruction(iid, path=db_path)             # 두 번 확인
    db.submit_report(iid, "재검수", "보고", path=db_path)
    with pytest.raises(WorkflowError):
        db.submit_report(iid, "재검수", "보고2", path=db_path)    # 두 번 보고
    db.approve_report(iid, path=db_path)
    for fn in (db.acknowledge_instruction, db.approve_report):
        with pytest.raises(WorkflowError):
            fn(iid, path=db_path)                                 # 완료 후 어떤 전환도 불가
    assert db.get_instruction(iid, path=db_path)["STATUS"] == "완료"


def test_unknown_instruction_and_invalid_inputs(db_path):
    with pytest.raises(WorkflowError):
        db.acknowledge_instruction(999999, path=db_path)
    with pytest.raises(WorkflowError):
        db.send_instruction("C001", "", "본문", path=db_path)
    with pytest.raises(WorkflowError):
        db.send_instruction("C001", "제목", "본문", priority="최상", path=db_path)
    with pytest.raises(WorkflowError):
        db.send_instruction("C999", "제목", "본문", path=db_path)


def test_seeded_instructions_states(db_path):
    rows = {r["CENTER_CODE"]: r for r in db.instructions(path=db_path)}
    assert rows["C003"]["STATUS"] == "지시완료"
    assert rows["C005"]["STATUS"] == "센터확인중"
    assert db.instructions(status="지시완료", path=db_path)[0]["CENTER_CODE"] == "C003"


# ------------------------------------------------------------------ 이벤트 조치
def _open_event(db_path, center="C003", min_remain=3):
    for e in db.events(center, only_open=True, path=db_path):
        if e["REMAIN_QTY"] >= min_remain and e["RESOLVED_QTY"] == 0:
            return e
    raise AssertionError("조건에 맞는 미처리 이벤트가 없음")


def test_resolve_event_partial_then_complete(db_path):
    ev = _open_event(db_path)
    eid, qty = ev["EVENT_ID"], ev["EVENT_QTY"]
    r1 = db.resolve_event(eid, "재검수", qty - 1, note="일부", path=db_path)
    assert r1["STATUS"] == "부분처리" and r1["RESOLVED_QTY"] == qty - 1 and r1["RESOLVED_AT"] is None
    assert len(db.event_resolves(eid, path=db_path)) == 1

    r2 = db.resolve_event(eid, "재검수", 1, path=db_path)
    assert r2["STATUS"] == "처리완료" and r2["RESOLVED_QTY"] == qty and r2["RESOLVED_AT"]
    assert len(db.event_resolves(eid, path=db_path)) == 2
    assert all(e["EVENT_ID"] != eid for e in db.events(only_open=True, path=db_path))


def test_resolve_event_rejects_over_quantity(db_path):
    ev = _open_event(db_path)
    eid, qty = ev["EVENT_ID"], ev["EVENT_QTY"]
    with pytest.raises(WorkflowError):
        db.resolve_event(eid, "재검수", qty + 1, path=db_path)
    with pytest.raises(WorkflowError):
        db.resolve_event(eid, "재검수", 0, path=db_path)
    with pytest.raises(WorkflowError):
        db.resolve_event(eid, "", 1, path=db_path)
    # 거부된 조치는 아무것도 남기지 않는다
    after = [e for e in db.events(only_open=True, path=db_path) if e["EVENT_ID"] == eid][0]
    assert after["RESOLVED_QTY"] == 0 and after["STATUS"] == "미처리"
    assert db.event_resolves(eid, path=db_path) == []


def test_resolve_event_rejects_closed(db_path):
    ev = _open_event(db_path)
    db.resolve_event(ev["EVENT_ID"], "폐기", ev["EVENT_QTY"], path=db_path)
    with pytest.raises(WorkflowError):
        db.resolve_event(ev["EVENT_ID"], "폐기", 1, path=db_path)
    with pytest.raises(WorkflowError):
        db.resolve_event(999999, "폐기", 1, path=db_path)


def test_overview_reflects_resolution(db_path):
    before = {r["center_code"]: r for r in db.overview(path=db_path)}["C003"]
    ev = _open_event(db_path)
    db.resolve_event(ev["EVENT_ID"], "재검수", ev["EVENT_QTY"], path=db_path)
    after = {r["center_code"]: r for r in db.overview(path=db_path)}["C003"]
    assert after["open_event_count"] == before["open_event_count"] - 1


# ------------------------------------------------------------------ 초안
def test_draft_approve_creates_instruction_once(db_path):
    run_id = db.start_run("manual", mode="rules", path=db_path)
    did = db.add_draft("C003", "초안 제목", "초안 본문", "긴급", fingerprint="abc123",
                       evidence_ids=["KPI:C003:2026-10-07", "EVENT:1"], run_id=run_id, path=db_path)
    assert [d["DRAFT_ID"] for d in db.drafts(path=db_path)] == [did]
    assert db.get_draft(did, path=db_path)["EVIDENCE_IDS"] == ["KPI:C003:2026-10-07", "EVENT:1"]

    n_before = len(db.instructions(path=db_path))
    iid = db.approve_draft(did, title="수정된 제목", path=db_path)
    inst = db.get_instruction(iid, path=db_path)
    assert inst["SOURCE"] == "에이전트초안" and inst["DRAFT_ID"] == did and inst["STATUS"] == "지시완료"
    assert inst["TITLE"] == "수정된 제목" and inst["BODY"] == "초안 본문" and inst["PRIORITY"] == "긴급"
    d = db.get_draft(did, path=db_path)
    assert d["STATUS"] == "승인" and d["INSTRUCTION_ID"] == iid and d["DECIDED_AT"]
    assert len(db.instructions(path=db_path)) == n_before + 1

    # 두 번째 승인은 거부되고 지시는 더 생기지 않는다
    with pytest.raises(WorkflowError):
        db.approve_draft(did, path=db_path)
    with pytest.raises(WorkflowError):
        db.reject_draft(did, path=db_path)
    assert len(db.instructions(path=db_path)) == n_before + 1
    assert db.drafts(path=db_path) == []
    assert len(db.drafts(status="승인", path=db_path)) == 1


def test_draft_reject(db_path):
    did = db.add_draft("C005", "초안", "본문", "보통", fingerprint="f1", path=db_path)
    n_before = len(db.instructions(path=db_path))
    db.reject_draft(did, note="근거 부족", path=db_path)
    d = db.get_draft(did, path=db_path)
    assert d["STATUS"] == "반려" and d["DECISION_NOTE"] == "근거 부족" and d["INSTRUCTION_ID"] is None
    with pytest.raises(WorkflowError):
        db.approve_draft(did, path=db_path)
    assert len(db.instructions(path=db_path)) == n_before


def test_add_draft_validation(db_path):
    with pytest.raises(WorkflowError):
        db.add_draft("C001", "제목", "본문", "최상", fingerprint="f", path=db_path)
    with pytest.raises(WorkflowError):
        db.add_draft("C001", "제목", "본문", "보통", fingerprint="", path=db_path)
    with pytest.raises(WorkflowError):
        db.approve_draft(999999, path=db_path)


# ------------------------------------------------------------------ 실행 기록
def test_start_and_finish_run(db_path):
    rid = db.start_run("scheduler", mode="agent", model="claude-sonnet-5-5", question="왜?", scope_codes=["C003"], path=db_path)
    r = db.get_run(rid, path=db_path)
    assert r["STATUS"] == "running" and r["SCOPE"] == ["C003"] and r["FINISHED_AT"] is None

    db.finish_run(rid, "success", report={"summary": "요약", "findings": [], "instruction_drafts": []},
                  tool_log=[{"tool": "get_operating_snapshot"}], detected=[{"center_code": "C003"}],
                  num_turns=3, cost_usd=0.01, session_id="s-1", path=db_path)
    r = db.runs(path=db_path)[0]
    assert r["RUN_ID"] == rid and r["STATUS"] == "success" and r["FINISHED_AT"]
    assert r["REPORT"]["summary"] == "요약" and r["TOOL_LOG"][0]["tool"] == "get_operating_snapshot"
    assert r["DETECTED"] == [{"center_code": "C003"}] and r["NUM_TURNS"] == 3 and r["DURATION_MS"] >= 0

    with pytest.raises(WorkflowError):
        db.finish_run(rid, "failed", path=db_path)      # 이미 종료된 실행
    with pytest.raises(WorkflowError):
        db.finish_run(rid, "running", path=db_path)
    with pytest.raises(WorkflowError):
        db.start_run("x", mode="llm", path=db_path)


def test_failed_run_records_error_and_alert(db_path):
    rid = db.start_run("manual", path=db_path)
    aid = db.add_alert(rid, "C003", "위험", ["정시출고율 92.4%"], "fp1", targeted=True, path=db_path)
    db.finish_run(rid, "failed", error_message="로그인 필요", path=db_path)
    r = db.get_run(rid, path=db_path)
    assert r["STATUS"] == "failed" and r["ERROR_MESSAGE"] == "로그인 필요"
    al = db.alerts(run_id=rid, path=db_path)
    assert len(al) == 1 and al[0]["ALERT_ID"] == aid and al[0]["REASONS"] == ["정시출고율 92.4%"] and al[0]["TARGETED"] == 1
