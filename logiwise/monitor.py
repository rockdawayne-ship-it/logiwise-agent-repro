"""모니터링 한 사이클 오케스트레이션 (STEP0_PLAN 1.7).

한 사이클 = ``run_cycle()`` 1회:

1. ``db.start_run`` 으로 AGENT_RUN(running) 생성
2. ``rules.detect_anomalies()`` 로 주의·위험 센터 탐지 → **전부** ``db.add_alert`` 로 MON_ALERT 기록
3. dedupe: 대기(WF_DRAFT.status='대기') 초안 중 같은 센터·같은 지문이 있으면 ``건너뜀_중복``.
   남은 것 중 ``settings.MONITOR["max_centers_per_cycle"]`` 개만 ``대상``, 나머지 ``건너뜀_한도`` (다음 사이클 백로그)
4. 대상 0건 → ``db.finish_run(status='skipped')`` 후 반환
5. 질문 생성(기준일 + 대상 센터별 사유) → ``agent.analyze(question, scope=대상)`` 또는
   ``use_agent=False`` 면 ``rules.rule_report(targets)`` (LLM 미사용, source='rules')
6. 보고서의 초안을 대상 센터의 지문과 함께 ``db.add_draft`` 로 WF_DRAFT 적재
7. ``db.finish_run('success')``

예외
- :class:`agent.AgentUnavailable` → ``finish_run('failed', error_message)``, 초안 0건.
- 그 밖의 예외도 failed 로 기록하고 다시 던지지 않는다(스케줄러 루프 유지). ``CycleResult.error`` 로 돌려준다.

이 모듈은 Streamlit 을 import 하지 않는다. 쓰기는 전부 ``db.py`` 함수로만 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import agent, db, rules, settings
from .models import AgentReport
from .settings import normalize_code

log = logging.getLogger(__name__)

# MON_ALERT.SKIP_REASON / 콘솔 표시용 처리 구분 (STEP0_PLAN 2.2 DISPOSITION)
DISPOSITION_TARGET = "대상"
DISPOSITION_SKIP_DUP = "건너뜀_중복"
DISPOSITION_SKIP_LIMIT = "건너뜀_한도"

MODE_AGENT = "agent"
MODE_RULES = "rules"


@dataclass
class CycleResult:
    """``run_cycle`` 의 반환값.

    - ``status``: success / failed / skipped
    - ``mode``: agent / rules (초안 ``source`` 배지와 같다)
    - ``detected``: 탐지 행 요약(센터·등급·사유·지문·처리 구분). MON_ALERT 와 1:1
    - ``targets``: 분석 대상 센터 코드(탐지 정렬 순서 유지)
    - ``skipped``: ``{센터코드: 건너뜀 사유}``
    - ``report``: 검증을 통과한 보고서. 실패·skipped 면 None
    - ``drafts``: 생성된 WF_DRAFT ID 목록
    - ``error``: 실패 시 한국어 사유
    """

    run_id: int
    status: str
    mode: str
    detected: list[dict[str, Any]] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    report: AgentReport | None = None
    drafts: list[int] = field(default_factory=list)
    error: str | None = None
    question: str | None = None
    kpi_day: str | None = None

    def summary_line(self) -> str:
        """스케줄러 한 줄 로그용 요약."""
        parts = [
            f"run_id={self.run_id}", f"status={self.status}", f"mode={self.mode}",
            f"detected={len(self.detected)}", f"targets={len(self.targets)}",
            f"skipped={len(self.skipped)}", f"drafts={len(self.drafts)}",
        ]
        line = " ".join(parts)
        if self.targets:
            line += " | 대상 " + ",".join(self.targets)
        if self.skipped:
            line += " | 건너뜀 " + ",".join(f"{c}({r.replace('건너뜀_', '')})" for c, r in self.skipped.items())
        if self.error:
            line += f" | 오류 {self.error}"
        return line


# ------------------------------------------------------------------ 내부 단계
def _max_centers() -> int:
    try:
        n = int(settings.MONITOR.get("max_centers_per_cycle", 2))
    except (TypeError, ValueError):
        n = 2
    return max(1, n)


def _waiting_fingerprints(path: str | None) -> dict[str, set[str]]:
    """센터별 '대기' 초안 지문 집합. dedupe 비교 키."""
    out: dict[str, set[str]] = {}
    for d in db.drafts(status="대기", path=path):
        out.setdefault(d["CENTER_CODE"], set()).add(d["FINGERPRINT"])
    return out


def plan_targets(detected: list[dict[str, Any]], path: str | None = None,
                 max_centers: int | None = None) -> dict[str, str]:
    """탐지 행마다 처리 구분을 정한다: ``{센터코드: 대상|건너뜀_중복|건너뜀_한도}``.

    - 대기 초안 중 같은 센터·같은 지문 → 건너뜀_중복 (사람이 아직 안 본 초안이 있고 상황도 그대로)
    - 대기 초안이 있어도 지문이 다르면 대상 (상황이 바뀌었으니 새 진단 필요)
    - 반려·승인된 초안은 '대기' 가 아니므로 dedupe 에 걸리지 않는다
    - 남은 것 중 앞에서 ``max_centers`` 개만 대상, 나머지는 건너뜀_한도 (탐지 정렬 = 위험 우선)
    """
    limit = max_centers if max_centers is not None else _max_centers()
    waiting = _waiting_fingerprints(path)
    plan: dict[str, str] = {}
    n_target = 0
    for row in detected:
        code = row["center_code"]
        if row["fingerprint"] in waiting.get(code, set()):
            plan[code] = DISPOSITION_SKIP_DUP
        elif n_target < limit:
            plan[code] = DISPOSITION_TARGET
            n_target += 1
        else:
            plan[code] = DISPOSITION_SKIP_LIMIT
    return plan


def build_question(kpi_day: str, targets: list[dict[str, Any]]) -> str:
    """에이전트에 보낼 질문. 기준일 + 대상 센터별 등급·사유 + 요청 사항."""
    lines = [
        f"기준일 {kpi_day} 운영 모니터링에서 규칙 기반 탐지로 아래 센터가 주의·위험으로 판정됐다.",
        "각 센터의 원인 가설과 지시 초안을 작성해 줘. 분석 범위는 아래 센터로 한정한다.",
        "",
    ]
    for r in targets:
        name = r.get("center_name") or ""
        reasons = "; ".join(r.get("reasons") or []) or "사유 없음"
        bns = ",".join(r.get("bottlenecks") or []) or "없음"
        lines.append(f"- {r['center_code']} {name} [{r['status']}] 병목: {bns} / 사유: {reasons}")
    lines += [
        "",
        "요청: get_operating_snapshot 으로 등급을 확인한 뒤, 각 센터의 get_center_detail 을 조회해 근거를 모으고,",
        "get_pending_drafts 로 중복을 확인한 다음 센터별 finding 1건과 지시 초안 1건을 작성한다.",
    ]
    return "\n".join(lines)


def _detected_summary(rows: list[dict[str, Any]], plan: dict[str, str]) -> list[dict[str, Any]]:
    """AGENT_RUN.DETECTED_JSON 과 CycleResult.detected 에 넣을 요약(직렬화 가능한 값만)."""
    return [
        {
            "center_code": r["center_code"],
            "center_name": r.get("center_name"),
            "status": r["status"],
            "on_time_rate": r.get("on_time_rate"),
            "bottlenecks": list(r.get("bottlenecks") or []),
            "reasons": list(r.get("reasons") or []),
            "open_event_ids": list(r.get("open_event_ids") or []),
            "fingerprint": r["fingerprint"],
            "disposition": plan.get(r["center_code"]),
        }
        for r in rows
    ]


def _store_drafts(run_id: int, report: AgentReport, targets: list[dict[str, Any]],
                  source: str, path: str | None) -> list[int]:
    """보고서의 초안을 대상 센터 지문과 함께 WF_DRAFT 에 적재한다. 대상 밖 센터의 초안은 버린다."""
    fp_by_center = {r["center_code"]: r["fingerprint"] for r in targets}
    ids: list[int] = []
    for d in report.instruction_drafts:
        fp = fp_by_center.get(d.center_code)
        if fp is None:   # validate_report 가 막지만, 규칙 경로·외부 보고서를 위해 한 번 더 방어
            log.warning("대상 밖 센터 %s 의 초안은 적재하지 않음", d.center_code)
            continue
        ids.append(db.add_draft(
            d.center_code, d.title, d.body, d.priority, fp,
            evidence_ids=list(d.evidence_ids), run_id=run_id, source=source, path=path,
        ))
    return ids


# ------------------------------------------------------------------ 사이클
def run_cycle(trigger: str = "scheduler", use_agent: bool = True, path: str | None = None,
              timeout_seconds: float | None = None) -> CycleResult:
    """모니터링 한 사이클. 예외를 밖으로 던지지 않는다(결과의 ``status``/``error`` 로 보고).

    - ``trigger``: scheduler / console / cli / test (AGENT_RUN.TRIGGER)
    - ``use_agent``: True 면 ``agent.analyze``(실제 Claude 호출, 비용 발생), False 면 ``rules.rule_report``
    - ``path``: DB 경로. 생략 시 ``settings.get_db_path()``
    """
    mode = MODE_AGENT if use_agent else MODE_RULES
    model = str(settings.AGENT.get("model")) if use_agent else None
    run_id = db.start_run(trigger, mode=mode, model=model, path=path)

    try:
        # 2) 탐지 → 전수 MON_ALERT 기록
        detected_rows = rules.detect_anomalies(path=path)
        plan = plan_targets(detected_rows, path=path)
        for r in detected_rows:
            disp = plan[r["center_code"]]
            db.add_alert(
                run_id, r["center_code"], r["status"], list(r.get("reasons") or []), r["fingerprint"],
                targeted=(disp == DISPOSITION_TARGET),
                skip_reason=None if disp == DISPOSITION_TARGET else disp,
                path=path,
            )
        detected = _detected_summary(detected_rows, plan)
        target_rows = [r for r in detected_rows if plan[r["center_code"]] == DISPOSITION_TARGET]
        targets = [r["center_code"] for r in target_rows]
        skipped = {c: d for c, d in plan.items() if d != DISPOSITION_TARGET}
        kpi_day = detected_rows[0]["kpi_day"] if detected_rows else settings.today_kst()

        # 4) 대상 없음 → skipped
        if not targets:
            db.finish_run(run_id, "skipped", detected=detected, path=path)
            return CycleResult(run_id, "skipped", mode, detected, targets, skipped, kpi_day=kpi_day)

        # 5) 질문 → 분석
        question = build_question(kpi_day, target_rows)
        if use_agent:
            try:
                result = agent.analyze(question, targets, path=path, timeout_seconds=timeout_seconds)
            except agent.AgentUnavailable as exc:
                # 보고서는 폐기, 초안 0건. 도구 로그·세션·턴·비용은 진단용으로 남긴다.
                db.finish_run(
                    run_id, "failed", error_message=exc.reason_ko, detected=detected, question=question,
                    scope_codes=targets, tool_log=exc.tool_log or None, session_id=exc.session_id,
                    model=exc.model, num_turns=exc.num_turns, cost_usd=exc.cost_usd,
                    duration_ms=exc.duration_ms, path=path,
                )
                return CycleResult(run_id, "failed", mode, detected, targets, skipped,
                                   error=exc.reason_ko, question=question, kpi_day=kpi_day)
            report = result.report
            meta: dict[str, Any] = dict(
                tool_log=result.tool_log, session_id=result.session_id, model=result.model,
                num_turns=result.num_turns, cost_usd=result.cost_usd, duration_ms=result.duration_ms,
            )
        else:
            report = rules.rule_report(target_rows, path=path)
            meta = {}

        # 6) 초안 적재 → 7) 성공 마감
        draft_ids = _store_drafts(run_id, report, target_rows, source=mode, path=path)
        db.finish_run(run_id, "success", report=report.model_dump(), detected=detected,
                      question=question, scope_codes=targets, path=path, **meta)
        return CycleResult(run_id, "success", mode, detected, targets, skipped,
                           report=report, drafts=draft_ids, question=question, kpi_day=kpi_day)

    except Exception as exc:  # noqa: BLE001 - 스케줄러 루프를 지키기 위해 모든 예외를 failed 로 기록
        log.exception("run_cycle 예상 밖 오류")
        msg = f"예상 밖 오류({type(exc).__name__})"
        try:
            db.finish_run(run_id, "failed", error_message=msg, path=path)
        except Exception:  # 이미 종료된 실행 등. 기록 실패는 삼킨다.
            log.exception("failed 기록 실패")
        return CycleResult(run_id, "failed", mode, error=msg)
