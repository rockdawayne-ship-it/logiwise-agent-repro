"""판단 계층 테스트(Step 3): AgentReport 스키마, 도구 핸들러, validate_report 통과/거부, analyze 실패 기록.

실제 Claude 호출은 하지 않는다. ``analyze`` 는 항상 monkeypatch 한다.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from logiwise import agent, db, rules
from logiwise.agent import AgentUnavailable, validate_report
from logiwise.models import AgentReport, Finding, InstructionDraft


# ------------------------------------------------------------------ 헬퍼
def _call(handler, args: dict | None = None) -> dict:
    """async 도구 핸들러를 동기로 호출해 JSON 텍스트를 파싱한 dict(또는 오류 결과)로 돌려준다."""
    res = asyncio.run(handler(args or {}))
    if res.get("is_error"):
        return res
    return json.loads(res["content"][0]["text"])


def _collect(db_path: str, scope: list[str], call_snapshot=True, call_detail=True, call_drafts=False):
    """도구를 실제로 호출해 evidence/tool_log 를 채운 뒤 (evidence, tool_log, statuses, detail) 를 돌려준다."""
    evidence: dict[str, str] = {}
    tool_log: list[dict] = []
    server = agent.build_tools(scope, evidence, tool_log, db_path)
    h = agent.tool_handlers(server)
    snap = _call(h[agent.TOOL_SNAPSHOT]) if call_snapshot else None
    details = {c: _call(h[agent.TOOL_DETAIL], {"center_code": c}) for c in scope} if call_detail else {}
    if call_drafts:
        _call(h[agent.TOOL_DRAFTS])
    statuses = {c["center_code"]: c["status"] for c in (snap or {}).get("centers", [])}
    return evidence, tool_log, statuses, details


def _finding(center: str, severity: str, ids: list[str]) -> Finding:
    return Finding(center_code=center, severity=severity, observation="관측", hypothesis="…로 추정",
                   recommendation="권장", evidence_ids=ids)


def _draft(center: str, ids: list[str], priority: str = "높음") -> InstructionDraft:
    return InstructionDraft(center_code=center, title="제목", body="본문", priority=priority, evidence_ids=ids)


def _good_report(db_path: str) -> tuple[AgentReport, dict, list]:
    """C003 범위에서 도구를 호출하고, 그 결과만으로 만든 유효한 보고서."""
    evidence, tool_log, statuses, details = _collect(db_path, ["C003"])
    kpi_id = details["C003"]["center"]["evidence_id"]
    event_ids = [e["evidence_id"] for e in details["C003"]["open_events"]][:2]
    ids = [kpi_id, *event_ids]
    rep = AgentReport(
        summary="기준일 · 샘플 데이터",
        findings=[_finding("C003", statuses["C003"], ids)],
        instruction_drafts=[_draft("C003", ids)],
    )
    return rep, evidence, tool_log


# ------------------------------------------------------------------ 스키마
def test_schema_literals_and_lengths():
    ok = _finding("C003", "위험", ["KPI:C003:2026-10-07"])
    assert ok.severity == "위험"
    with pytest.raises(ValidationError):
        _finding("C003", "심각", ["KPI:C003:2026-10-07"])                    # Literal 밖
    with pytest.raises(ValidationError):
        _finding("C3", "위험", ["KPI:C003:2026-10-07"])                       # 코드 패턴
    with pytest.raises(ValidationError):
        _finding("C003", "위험", [])                                           # evidence_ids 최소 1
    with pytest.raises(ValidationError):
        _draft("C003", ["EVENT:1"], priority="매우높음")                      # Priority Literal
    with pytest.raises(ValidationError):
        InstructionDraft(center_code="C003", title="t" * 121, body="b", priority="보통", evidence_ids=["EVENT:1"])
    with pytest.raises(ValidationError):
        InstructionDraft(center_code="C003", title="t", body="b" * 3001, priority="보통", evidence_ids=["EVENT:1"])
    f = _finding("C003", "위험", ["EVENT:1"])
    with pytest.raises(ValidationError):
        AgentReport(summary="s", findings=[f] * 8, instruction_drafts=[])     # findings ≤ 7
    with pytest.raises(ValidationError):
        AgentReport(summary="s", findings=[f], instruction_drafts=[_draft("C003", ["EVENT:1"])] * 4)  # drafts ≤ 3
    with pytest.raises(ValidationError):
        AgentReport(summary="s", findings=[], instruction_drafts=[], extra_field=1)  # extra="forbid"


def test_schema_is_used_as_output_format():
    schema = AgentReport.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"summary", "findings", "instruction_drafts"}
    assert "Finding" in schema["$defs"] and "InstructionDraft" in schema["$defs"]


# ------------------------------------------------------------------ 도구 핸들러
def test_tools_fill_evidence_and_tool_log(db_path):
    evidence, tool_log, statuses, details = _collect(db_path, ["C003", "C005"], call_drafts=True)
    assert [e["tool"] for e in tool_log] == [agent.TOOL_SNAPSHOT, agent.TOOL_DETAIL, agent.TOOL_DETAIL, agent.TOOL_DRAFTS]
    assert tool_log[0]["statuses"] == {"C003": "위험", "C005": "주의"}
    assert set(statuses) == {"C003", "C005"}                      # 범위 밖 센터는 스냅샷에 없음
    assert all(e["ok"] for e in tool_log)
    # 모든 근거 ID 는 접두사 형식이고 센터로 매핑된다
    assert evidence and all(k.startswith(agent.EVIDENCE_PREFIXES) for k in evidence)
    assert set(evidence.values()) == {"C003", "C005"}
    d = details["C003"]
    assert len(d["kpi_7d"]) == 7 and all(r["evidence_id"].startswith("KPI:C003:") for r in d["kpi_7d"])
    assert len(d["inout_7d"]) == 7 and all(r["evidence_id"].startswith("INOUT:C003:") for r in d["inout_7d"])
    assert d["open_events"] and all(r["evidence_id"].startswith("EVENT:") for r in d["open_events"])
    assert all(r["delay_level"] in ("녹", "노", "적") for r in d["open_events"])
    assert all(r["evidence_id"].startswith("WF:") for r in d["instructions"])
    assert "지시문이 아님" in d["note"]
    assert tool_log[1]["n_ids"] == len({r["evidence_id"] for r in d["kpi_7d"]} | {r["evidence_id"] for r in d["inout_7d"]}
                                       | {r["evidence_id"] for r in d["open_events"]} | {r["evidence_id"] for r in d["instructions"]}
                                       | {d["center"]["evidence_id"]})


def test_tool_rejects_out_of_scope_center(db_path):
    evidence, tool_log = {}, []
    h = agent.tool_handlers(agent.build_tools(["C003"], evidence, tool_log, db_path))
    res = _call(h[agent.TOOL_DETAIL], {"center_code": "C005"})
    assert res.get("is_error") is True and "범위 밖" in res["content"][0]["text"]
    res = _call(h[agent.TOOL_DRAFTS], {"center_code": "C005"})
    assert res.get("is_error") is True
    res = _call(h[agent.TOOL_DETAIL], {"center_code": "ZZZ"})
    assert res.get("is_error") is True
    assert evidence == {} and all(e["ok"] is False for e in tool_log)


def test_pending_drafts_tool_lists_waiting_only(db_path):
    fp = "a" * 16
    d1 = db.add_draft("C003", "초안1", "본문", "높음", fp, ["KPI:C003:x"], path=db_path)
    d2 = db.add_draft("C003", "초안2", "본문", "보통", fp, path=db_path)
    db.reject_draft(d2, "반려", path=db_path)
    evidence, tool_log = {}, []
    h = agent.tool_handlers(agent.build_tools(["C003", "C005"], evidence, tool_log, db_path))
    res = _call(h[agent.TOOL_DRAFTS])
    assert [r["draft_id"] for r in res["pending_drafts"]] == [d1]
    assert evidence == {f"DRAFT:{d1}": "C003"}


# ------------------------------------------------------------------ validate_report: 통과 1 + 거부 5
def test_validate_report_passes(db_path):
    rep, evidence, tool_log = _good_report(db_path)
    assert validate_report(rep, ["C003"], evidence, tool_log) is rep


def test_validate_rejects_without_snapshot(db_path):
    rep, evidence, tool_log = _good_report(db_path)
    no_snap = [e for e in tool_log if e["tool"] != agent.TOOL_SNAPSHOT]
    with pytest.raises(AgentUnavailable, match="get_operating_snapshot"):
        validate_report(rep, ["C003"], evidence, no_snap)


def test_validate_rejects_out_of_scope_center(db_path):
    rep, evidence, tool_log = _good_report(db_path)
    with pytest.raises(AgentUnavailable, match="범위 밖"):
        validate_report(rep, ["C005"], evidence, tool_log)


def test_validate_rejects_unknown_evidence_id(db_path):
    rep, evidence, tool_log = _good_report(db_path)
    bad = rep.model_copy(deep=True)
    bad.findings[0].evidence_ids.append("EVENT:999999")       # 형식은 맞지만 조회 안 됨
    with pytest.raises(AgentUnavailable, match="조회되지 않은 근거"):
        validate_report(bad, ["C003"], evidence, tool_log)
    bad2 = rep.model_copy(deep=True)
    bad2.findings[0].evidence_ids = ["만든ID"]               # 형식 자체가 틀림
    with pytest.raises(AgentUnavailable, match="조회되지 않은 근거"):
        validate_report(bad2, ["C003"], evidence, tool_log)


def test_validate_rejects_center_evidence_mismatch(db_path):
    evidence, tool_log, statuses, details = _collect(db_path, ["C003", "C005"])
    c005_id = details["C005"]["center"]["evidence_id"]
    rep = AgentReport(summary="s", findings=[_finding("C003", statuses["C003"], [c005_id])], instruction_drafts=[])
    with pytest.raises(AgentUnavailable, match="C005 의 것"):
        validate_report(rep, ["C003", "C005"], evidence, tool_log)


def test_validate_rejects_changed_severity(db_path):
    rep, evidence, tool_log = _good_report(db_path)
    bad = rep.model_copy(deep=True)
    bad.findings[0].severity = "정상"                          # 규칙 등급은 위험
    with pytest.raises(AgentUnavailable, match="규칙 등급"):
        validate_report(bad, ["C003"], evidence, tool_log)


def test_validate_rejects_draft_without_finding(db_path):
    evidence, tool_log, statuses, details = _collect(db_path, ["C003", "C005"])
    ids3 = [details["C003"]["center"]["evidence_id"]]
    ids5 = [details["C005"]["center"]["evidence_id"]]
    rep = AgentReport(summary="s", findings=[_finding("C003", statuses["C003"], ids3)],
                      instruction_drafts=[_draft("C005", ids5)])
    with pytest.raises(AgentUnavailable, match="진단\\(finding\\) 없는"):
        validate_report(rep, ["C003", "C005"], evidence, tool_log)


def test_validate_rejects_empty_findings(db_path):
    _, evidence, tool_log = _good_report(db_path)
    rep = AgentReport(summary="s", findings=[], instruction_drafts=[])
    with pytest.raises(AgentUnavailable, match="findings 가 비어"):
        validate_report(rep, ["C003"], evidence, tool_log)


def test_rule_report_passes_validation_with_real_tool_calls(db_path):
    """규칙 기반 보고서(LLM 대체 경로)도 같은 검증을 통과해야 한다."""
    targets = [r for r in rules.detect_anomalies(path=db_path) if r["center_code"] in ("C003", "C005")]
    rep = rules.rule_report(targets, path=db_path)
    evidence, tool_log, _, _ = _collect(db_path, ["C003", "C005"])
    assert validate_report(rep, ["C003", "C005"], evidence, tool_log) is rep


# ------------------------------------------------------------------ analyze_and_record (Claude 호출 없이)
def test_analyze_and_record_success_and_failure(db_path, monkeypatch):
    rep, evidence, tool_log = _good_report(db_path)
    good = agent.AgentResult(report=rep, tool_log=tool_log, session_id="sess-1", model="m", num_turns=3,
                             cost_usd=0.01, duration_ms=1234)
    monkeypatch.setattr(agent, "analyze", lambda *a, **k: good)
    rec = agent.analyze_and_record("질문", ["C003"], trigger="test", path=db_path)
    run = db.get_run(rec.run_id, path=db_path)
    assert rec.status == "success" and run["STATUS"] == "success"
    assert run["REPORT"]["findings"][0]["center_code"] == "C003"
    assert run["SESSION_ID"] == "sess-1" and run["NUM_TURNS"] == 3 and run["COST_USD"] == 0.01 and run["DURATION_MS"] == 1234
    assert run["SCOPE"] == ["C003"] and run["QUESTION"] == "질문" and run["TRIGGER"] == "test" and run["MODE"] == "agent"
    assert [e["tool"] for e in run["TOOL_LOG"]] == [e["tool"] for e in tool_log]

    def _fail(*a, **k):
        raise AgentUnavailable("로그인 필요: 테스트", kind="로그인 필요")
    monkeypatch.setattr(agent, "analyze", _fail)
    rec2 = agent.analyze_and_record("질문", ["C003"], trigger="test", path=db_path)
    run2 = db.get_run(rec2.run_id, path=db_path)
    assert rec2.status == "failed" and rec2.result is None and "로그인 필요" in rec2.error
    assert run2["STATUS"] == "failed" and run2["REPORT"] is None and run2["ERROR_MESSAGE"] == "로그인 필요: 테스트"
    assert run2["FINISHED_AT"]


def test_exception_translation_hides_original_text():
    from claude_agent_sdk import CLINotFoundError, ProcessError

    e = agent._translate_exception(asyncio.TimeoutError(), 120)
    assert e.kind == "타임아웃" and "120" in e.reason_ko
    e = agent._translate_exception(CLINotFoundError("Claude Code not found at /x/y"), 120)
    assert e.kind == "CLI 없음" and "/x/y" not in e.reason_ko
    e = agent._translate_exception(ProcessError("exit 1", exit_code=1, stderr="Error: Not logged in. Please run /login"), 120)
    assert e.kind == "로그인 필요" and "/login" not in e.reason_ko
    e = agent._translate_exception(ProcessError("boom", exit_code=2, stderr="segfault at 0xdead"), 120)
    assert e.kind == "기타" and "0xdead" not in e.reason_ko


def test_system_prompt_contains_required_rules():
    p = agent.SYSTEM_PROMPT
    for needle in (agent.TOOL_SNAPSHOT, "그대로 복사", "추정", "미수금", "새로 만들", "검토", "지시문이 아니다", "kpi_day", "샘플 데이터"):
        assert needle in p
    for item in agent.REPORT_ITEMS:
        assert item in p


def test_failed_run_keeps_tool_log_but_discards_report(db_path, monkeypatch):
    """검증 실패 시 보고서는 버리되 도구 로그·세션 메타는 AGENT_RUN 에 남는다."""
    _, evidence, tool_log = _good_report(db_path)

    def _fail(*a, **k):
        exc = AgentUnavailable("보고서 검증 실패: 테스트", kind="검증 실패")
        exc.tool_log, exc.session_id, exc.model, exc.num_turns, exc.cost_usd, exc.duration_ms = tool_log, "s-9", "m", 4, 0.02, 999
        raise exc
    monkeypatch.setattr(agent, "analyze", _fail)
    rec = agent.analyze_and_record("질문", ["C003"], trigger="test", path=db_path)
    run = db.get_run(rec.run_id, path=db_path)
    assert run["STATUS"] == "failed" and run["REPORT"] is None
    assert [e["tool"] for e in run["TOOL_LOG"]] == [agent.TOOL_SNAPSHOT, agent.TOOL_DETAIL]
    assert run["SESSION_ID"] == "s-9" and run["NUM_TURNS"] == 4 and run["COST_USD"] == 0.02 and run["DURATION_MS"] == 999


# ==================================================================== Step 4: monitor / scheduler
from logiwise import monitor, scheduler  # noqa: E402


def _cycle(db_path: str, use_agent: bool = False, trigger: str = "test") -> monitor.CycleResult:
    return monitor.run_cycle(trigger=trigger, use_agent=use_agent, path=db_path)


def _waiting(db_path: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for d in db.drafts(status="대기", path=db_path):
        out.setdefault(d["CENTER_CODE"], []).append(d)
    return out


def test_rules_cycle_creates_drafts_and_records_everything(db_path):
    """규칙 사이클: 위험 1+주의 2 시드 → 2 대상(C003, C005)·1 한도(C006), 초안 2건, MON_ALERT 3건."""
    detected = rules.detect_anomalies(path=db_path)
    fp = {r["center_code"]: r["fingerprint"] for r in detected}

    res = _cycle(db_path)
    assert res.status == "success" and res.mode == "rules" and res.error is None
    assert res.targets == ["C003", "C005"]
    assert res.skipped == {"C006": monitor.DISPOSITION_SKIP_LIMIT}
    assert len(res.drafts) == 2 and res.report is not None
    assert "규칙 기반" in res.report.summary and res.question and "C003" in res.question and "C005" in res.question
    assert "C006" not in res.question

    # WF_DRAFT: 대상 센터 지문과 함께, source='rules'
    waiting = _waiting(db_path)
    assert set(waiting) == {"C003", "C005"}
    for code, rows in waiting.items():
        assert len(rows) == 1 and rows[0]["FINGERPRINT"] == fp[code] and rows[0]["SOURCE"] == "rules"
        assert rows[0]["RUN_ID"] == res.run_id and rows[0]["EVIDENCE_IDS"]
    assert waiting["C003"][0]["PRIORITY"] == "높음" and waiting["C005"][0]["PRIORITY"] == "보통"

    # MON_ALERT: 탐지 전수 기록 + 처리 구분
    alerts = {a["CENTER_CODE"]: a for a in db.alerts(res.run_id, path=db_path)}
    assert set(alerts) == {"C003", "C005", "C006"}
    assert alerts["C003"]["TARGETED"] == 1 and alerts["C003"]["SKIP_REASON"] is None and alerts["C003"]["CENTER_STATUS"] == "위험"
    assert alerts["C006"]["TARGETED"] == 0 and alerts["C006"]["SKIP_REASON"] == monitor.DISPOSITION_SKIP_LIMIT
    assert alerts["C003"]["FINGERPRINT"] == fp["C003"] and alerts["C003"]["REASONS"]

    # AGENT_RUN
    run = db.get_run(res.run_id, path=db_path)
    assert run["STATUS"] == "success" and run["MODE"] == "rules" and run["TRIGGER"] == "test"
    assert run["SCOPE"] == ["C003", "C005"] and run["QUESTION"] == res.question
    assert [d["center_code"] for d in run["DETECTED"]] == ["C003", "C005", "C006"]
    assert [d["disposition"] for d in run["DETECTED"]] == ["대상", "대상", "건너뜀_한도"]
    assert len(run["REPORT"]["instruction_drafts"]) == 2 and run["MODEL"] is None


def test_repeated_cycles_process_backlog_then_skip(db_path):
    """1회차 2 대상·1 한도 → 2회차 C006 백로그 처리(C003·C005 중복) → 3회차 skipped."""
    r1 = _cycle(db_path)
    assert r1.targets == ["C003", "C005"]

    r2 = _cycle(db_path)
    assert r2.status == "success" and r2.targets == ["C006"] and len(r2.drafts) == 1
    assert r2.skipped == {"C003": monitor.DISPOSITION_SKIP_DUP, "C005": monitor.DISPOSITION_SKIP_DUP}

    r3 = _cycle(db_path)
    assert r3.status == "skipped" and r3.targets == [] and r3.drafts == [] and r3.report is None
    assert set(r3.skipped) == {"C003", "C005", "C006"} and set(r3.skipped.values()) == {monitor.DISPOSITION_SKIP_DUP}
    run3 = db.get_run(r3.run_id, path=db_path)
    assert run3["STATUS"] == "skipped" and run3["REPORT"] is None and run3["FINISHED_AT"]
    # skipped 사이클도 탐지 전수를 MON_ALERT 에 남긴다 (왜 안 물어봤는지 보이도록)
    assert len(db.alerts(r3.run_id, path=db_path)) == 3
    assert all(a["TARGETED"] == 0 and a["SKIP_REASON"] == monitor.DISPOSITION_SKIP_DUP for a in db.alerts(r3.run_id, path=db_path))
    # 대기 초안은 센터당 1건 그대로
    assert {c: len(v) for c, v in _waiting(db_path).items()} == {"C003": 1, "C005": 1, "C006": 1}
    assert len(db.runs(path=db_path)) == 3


def test_rejected_draft_becomes_target_again_and_fingerprint_change_retargets(db_path):
    """반려 → 다음 사이클 재대상. 이벤트 처리로 지문이 바뀌면 대기 초안이 있어도 재대상."""
    for _ in range(3):
        _cycle(db_path)
    assert _cycle(db_path).status == "skipped"

    old = _waiting(db_path)["C003"][0]
    db.reject_draft(old["DRAFT_ID"], note="근거 보강 필요", path=db_path)
    r4 = _cycle(db_path)
    assert r4.status == "success" and r4.targets == ["C003"] and len(r4.drafts) == 1
    assert r4.skipped == {"C005": monitor.DISPOSITION_SKIP_DUP, "C006": monitor.DISPOSITION_SKIP_DUP}
    new = _waiting(db_path)["C003"][0]
    assert new["DRAFT_ID"] != old["DRAFT_ID"] and new["FINGERPRINT"] == old["FINGERPRINT"]   # 상황 동일 → 같은 지문

    # 같은 상황이면 다시 skipped
    assert _cycle(db_path).status == "skipped"

    # C003 이벤트 1건 처리 → 지문 변경 → 대기 초안이 있어도 다시 대상
    ev = db.events("C003", only_open=True, path=db_path)[0]
    db.resolve_event(ev["EVENT_ID"], "재검수", ev["REMAIN_QTY"], path=db_path)
    fp_after = {r["center_code"]: r["fingerprint"] for r in rules.detect_anomalies(path=db_path)}["C003"]
    assert fp_after != new["FINGERPRINT"]
    r6 = _cycle(db_path)
    assert r6.status == "success" and r6.targets == ["C003"]
    assert {d["FINGERPRINT"] for d in _waiting(db_path)["C003"]} == {new["FINGERPRINT"], fp_after}


def test_approving_cycle_draft_creates_agent_sourced_instruction(db_path):
    """사이클이 만든 초안을 사람이 승인 → WF_INSTRUCTION(source='에이전트초안') 1건, 1회만."""
    res = _cycle(db_path)
    draft = _waiting(db_path)["C003"][0]
    n_before = len(db.instructions("C003", path=db_path))

    iid = db.approve_draft(draft["DRAFT_ID"], path=db_path)
    inst = db.get_instruction(iid, path=db_path)
    assert inst["SOURCE"] == "에이전트초안" and inst["STATUS"] == "지시완료" and inst["DRAFT_ID"] == draft["DRAFT_ID"]
    assert inst["TITLE"] == draft["TITLE"] and inst["PRIORITY"] == draft["PRIORITY"] and inst["CENTER_CODE"] == "C003"
    assert len(db.instructions("C003", path=db_path)) == n_before + 1
    assert db.get_draft(draft["DRAFT_ID"], path=db_path)["STATUS"] == "승인"
    with pytest.raises(db.WorkflowError):
        db.approve_draft(draft["DRAFT_ID"], path=db_path)
    # 승인된 초안은 대기열에서 빠지고, 지시는 PRD 워크플로우를 그대로 탄다
    assert "C003" not in _waiting(db_path)
    assert db.acknowledge_instruction(iid, path=db_path) == "센터확인중"
    assert res.run_id == db.get_draft(draft["DRAFT_ID"], path=db_path)["RUN_ID"]


def test_agent_failure_records_failed_run_and_no_drafts(db_path, monkeypatch):
    """analyze 가 AgentUnavailable 을 던지면 run=failed, 초안 0건, MON_ALERT 는 기록."""
    calls: list[tuple] = []

    def _fail(question, scope_codes, path=None, timeout_seconds=None):
        calls.append((question, list(scope_codes)))
        exc = AgentUnavailable("로그인 필요: 테스트", kind="로그인 필요")
        exc.tool_log, exc.session_id, exc.num_turns = [{"tool": agent.TOOL_SNAPSHOT, "ok": True}], "s-f", 2
        raise exc
    monkeypatch.setattr(agent, "analyze", _fail)

    res = _cycle(db_path, use_agent=True)
    assert res.status == "failed" and res.mode == "agent" and res.drafts == [] and res.report is None
    assert "로그인 필요" in res.error and res.targets == ["C003", "C005"]
    assert calls and calls[0][1] == ["C003", "C005"] and "C003" in calls[0][0]
    run = db.get_run(res.run_id, path=db_path)
    assert run["STATUS"] == "failed" and run["MODE"] == "agent" and run["REPORT"] is None
    assert run["ERROR_MESSAGE"] == "로그인 필요: 테스트" and run["SESSION_ID"] == "s-f" and run["NUM_TURNS"] == 2
    assert run["TOOL_LOG"][0]["tool"] == agent.TOOL_SNAPSHOT and run["SCOPE"] == ["C003", "C005"]
    assert db.drafts(status=None, path=db_path) == []
    assert len(db.alerts(res.run_id, path=db_path)) == 3

    # 실패했으므로 대기 초안이 없고, 다음 사이클은 같은 센터를 다시 대상으로 잡는다
    res2 = _cycle(db_path, use_agent=True)
    assert res2.status == "failed" and res2.targets == ["C003", "C005"]


def test_agent_success_with_monkeypatch_stores_agent_drafts(db_path, monkeypatch):
    """analyze 가 보고서를 돌려주면 초안 source='agent', 실행 메타가 AGENT_RUN 에 남는다."""
    def _ok(question, scope_codes, path=None, timeout_seconds=None):
        targets = [r for r in rules.detect_anomalies(path=path) if r["center_code"] in scope_codes]
        rep = rules.rule_report(targets, path=path)
        return agent.AgentResult(report=rep, tool_log=[{"tool": agent.TOOL_SNAPSHOT, "ok": True}],
                                 session_id="s-ok", model="m", num_turns=5, cost_usd=0.03, duration_ms=777)
    monkeypatch.setattr(agent, "analyze", _ok)

    res = _cycle(db_path, use_agent=True)
    assert res.status == "success" and res.mode == "agent" and len(res.drafts) == 2
    waiting = _waiting(db_path)
    assert all(rows[0]["SOURCE"] == "agent" for rows in waiting.values())
    run = db.get_run(res.run_id, path=db_path)
    assert run["MODE"] == "agent" and run["MODEL"] == "m" and run["SESSION_ID"] == "s-ok"
    assert run["NUM_TURNS"] == 5 and run["COST_USD"] == 0.03 and run["DURATION_MS"] == 777
    assert run["REPORT"]["findings"][0]["center_code"] == "C003"
    # 두 번째는 중복으로 C006 만 대상
    assert _cycle(db_path, use_agent=True).targets == ["C006"]


def test_unexpected_exception_is_recorded_as_failed_without_raising(db_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("뜻밖")
    monkeypatch.setattr(agent, "analyze", _boom)
    res = _cycle(db_path, use_agent=True)
    assert res.status == "failed" and "RuntimeError" in res.error
    run = db.get_run(res.run_id, path=db_path)
    assert run["STATUS"] == "failed" and run["FINISHED_AT"] and db.drafts(path=db_path) == []


def test_plan_targets_respects_limit_and_waiting_fingerprint(db_path):
    detected = rules.detect_anomalies(path=db_path)
    assert list(monitor.plan_targets(detected, path=db_path, max_centers=1).values()) == ["대상", "건너뜀_한도", "건너뜀_한도"]
    assert list(monitor.plan_targets(detected, path=db_path, max_centers=5).values()) == ["대상", "대상", "대상"]
    c005 = next(r for r in detected if r["center_code"] == "C005")
    db.add_draft("C005", "t", "b", "보통", c005["fingerprint"], path=db_path)          # 같은 지문 대기
    db.add_draft("C003", "t", "b", "높음", "0" * 16, path=db_path)                    # 다른 지문 대기
    plan = monitor.plan_targets(detected, path=db_path, max_centers=2)
    assert plan == {"C003": "대상", "C005": "건너뜀_중복", "C006": "대상"}


def test_scheduler_once_rules_only_runs_one_cycle(db_path, capsys):
    assert scheduler.resolve_interval(10) == 30 and scheduler.resolve_interval(45.9) == 45
    assert scheduler.resolve_interval(None) == max(30, int(scheduler.settings.MONITOR["interval_seconds"]))
    rc = scheduler.main(["--once", "--rules-only", "--interval", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[cycle]" in out and "status=success" in out and "mode=rules" in out and "drafts=2" in out
    runs = db.runs(path=db_path)
    assert len(runs) == 1 and runs[0]["TRIGGER"] == "scheduler" and runs[0]["MODE"] == "rules"
    rc2 = scheduler.main(["--once", "--rules-only"])
    out2 = capsys.readouterr().out
    assert rc2 == 0 and "targets=1" in out2 and "C006" in out2 and "중복" in out2
