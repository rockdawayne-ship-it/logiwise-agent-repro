"""에이전트 보고서 구조화 출력 모델 (STEP0_PLAN 4장).

- ``rules.rule_report()`` (규칙 기반 대체 경로)와 ``agent.py`` (Claude 구조화 출력)가 **같은 모델**을 쓴다.
  화면·DB 적재 코드는 출처를 구분하지 않고 ``source`` 배지만 다르게 붙인다.
- ``extra="forbid"`` 로 임의 필드를 막는다. ``model_json_schema()`` 를 그대로 SDK ``output_format`` 에 넣는다.
- 이 모듈은 다른 logiwise 모듈을 import 하지 않는다(의존 방향 최하단).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["정상", "주의", "위험"]
Priority = Literal["보통", "높음", "긴급"]

CENTER_CODE_PATTERN = r"^C\d{3}$"


class Finding(BaseModel):
    """센터 1곳에 대한 진단. 관측(숫자)·가설(추정)·권장(행동)을 분리한다."""

    model_config = ConfigDict(extra="forbid")

    center_code: str = Field(pattern=CENTER_CODE_PATTERN)
    severity: Severity                                  # 반드시 rules.snapshot 의 status 와 동일
    observation: str = Field(min_length=1)              # 숫자로 확인된 사실만
    hypothesis: str = Field(min_length=1)               # 추정. "…로 추정" 표현
    recommendation: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class InstructionDraft(BaseModel):
    """지시 초안. 사람이 검토할 문서이며 발송이 아니다."""

    model_config = ConfigDict(extra="forbid")

    center_code: str = Field(pattern=CENTER_CODE_PATTERN)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=3000)
    priority: Priority
    evidence_ids: list[str] = Field(min_length=1)


class AgentReport(BaseModel):
    """한 사이클의 보고서. summary 첫 줄에 기준일과 '샘플 데이터' 표시."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    findings: list[Finding] = Field(max_length=7)
    instruction_drafts: list[InstructionDraft] = Field(max_length=3)
