# DESIGN — LOGIWISE 자율 모니터링 AI Agent 설계 노트

- 근거: `docs/PRD.md`(v1.0), `CLAUDE.md`, `docs/STEP0_PLAN.md`
- 상태: Step 1~6 구현 완료. `pytest tests -q` → **124 passed**. 실제 에이전트 사이클 1회 성공(`AGENT_RUN.RUN_ID=6`).
- 표기: PRD 에 있는 값은 **[PRD x.x]**, PRD 에 없어 제안한 값은 **[보완]**.

---

## 1. 왜 "자율 모니터링" 인가

PRD 의 본사 화면은 **사람이 들어와서 보는** 대시보드다. 정시출고율·미납·이상건수가 카드와 히트맵으로 보이지만, 누군가 화면을 열지 않으면 아무 일도 일어나지 않는다. 본사 페르소나의 일은 "이상 센터를 찾고 → 왜 그런지 보고 → 지시를 보내는" 반복 작업인데, 이 중 앞의 두 단계는 데이터만으로 상당 부분 기계화할 수 있다.

그래서 에이전트를 대시보드 **위에** 얹었다. 대시보드를 바꾸지 않고(PRD 화면 그대로), 주기적으로 같은 데이터를 읽어 "이상 센터 찾기"와 "원인 조회·진단·초안 작성"까지만 자동으로 하고, 마지막 "지시를 보내는" 결정은 사람에게 남긴다.

왜 완전 자동 발송이 아닌가:

1. **LLM 의 가설은 추정이다.** 샘플 데이터만으로도 "피킹 처리량이 줄어 미납이 누적된 것으로 추정" 같은 그럴듯한 가설이 나오지만, 현장 확인 없이 센터에 지시로 나가면 안 된다. 실제 실행(RUN_ID=6)의 finding 도 "데이터만으로 확정할 수 없다" 고 스스로 적었다.
2. **잘못된 지시의 비용이 비대칭이다.** 초안이 하나 더 쌓이는 것은 싸고, 틀린 지시가 센터에 나가는 것은 비싸다.
3. **PRD 워크플로우가 사람 중심이다.** 지시완료 → 센터확인중 → 조치중 → 완료 전환은 모두 사람의 버튼이다(PRD 5장). 에이전트는 이 상태 머신에 손대지 않는다.

따라서 "자율" 의 범위는 **탐지와 진단·초안까지**, 승인부터는 사람이다.

---

## 2. 세 계층 분리: 결정론 → 판단 → 승인

```
결정론 계층 (rules.py)        판단 계층 (agent.py)            승인 계층 (db.py + views/hq.py)
등급·병목·히트맵·지연색        Claude + 읽기 전용 도구 3개       초안 승인 = WF_INSTRUCTION 생성
이상 탐지 + 지문               관측·가설·권장·초안 (JSON)        사람만 누른다
LLM 없음                      등급을 바꿀 수 없음               에이전트 도구 없음
        │                              │                               │
        └──── monitor.run_cycle 이 한 사이클 안에서 ①→② 를 묶고 MON_ALERT / WF_DRAFT / AGENT_RUN 에 기록 ────┘
```

### 2.1 결정론 계층 — 숫자로 정해지는 것은 코드가 정한다

등급(정상/주의/위험/데이터 없음), 병목 5단계, 히트맵 색, 이벤트 지연색, 사유 문구, 지문. 전부 `rules.py` 의 순수 함수이고 임계값은 `config.toml [rules]` 에서만 온다. LLM 은 여기에 관여하지 않는다.

이유: 이 값들은 화면 색·탐지·초안 근거·테스트에 모두 쓰인다. LLM 이 정하면 사이클마다 다르게 나올 수 있고, 테스트할 수 없고, "왜 위험이냐" 에 답할 수 없다. 규칙으로 정하면 사이드바 "판단 기준" 표 한 장으로 설명이 끝난다.

### 2.2 판단 계층 — 숫자로 정해지지 않는 것만 LLM 에게

원인 가설, 센터가 취할 구체 행동, 사람이 읽을 지시 초안 문장. 이것은 규칙으로 쓰기 어렵고(규칙 경로는 "수치만으로 원인을 확정할 수 없음" 이라는 고정 문구만 낸다), LLM 이 잘하는 일이다.

단, LLM 에게 주는 것은 **읽기 전용 도구 3개** 뿐이다.

| 도구 | 역할 | 근거 ID |
|---|---|---|
| `get_operating_snapshot` | 범위 센터의 등급·병목·사유. **항상 첫 호출**. 여기의 `status` 가 `severity` 의 유일한 출처 | `KPI:{center}:{day}` |
| `get_center_detail` | 7일 KPI·입출고·미처리 이벤트·지시 이력 | `KPI:` `INOUT:` `EVENT:` `WF:` |
| `get_pending_drafts` | 이미 대기 중인 초안 — 같은 내용을 또 쓰지 않게 | `DRAFT:` |

발송·승인·상태 변경·이벤트 조치 도구는 만들지 않았다. SDK 옵션 `tools=[]` 로 파일·Bash·웹 내장 도구도 뺐고, `allowed_tools=["mcp__logiwise__*"]` 로 인프로세스 MCP 서버의 도구만 허용했다. `setting_sources=[]` 로 사용자 `CLAUDE.md`·프로젝트 설정이 프롬프트에 섞이지 않게 했다.

LLM 의 출력은 **두 번** 검증한다.

1. 형식: `output_format=json_schema` + pydantic `AgentReport`(`extra="forbid"`, 길이·Literal 제한).
2. 의미(`validate_report`): ① snapshot 미호출 ② 범위 밖 센터 ③ 조회되지 않은 근거 ID ④ 근거 ID 의 센터 ≠ finding 센터 ⑤ `severity` ≠ 규칙 등급 ⑥ finding 없는 센터의 초안 ⑦ findings 비어 있음. 하나라도 걸리면 보고서 **전체** 폐기(`AGENT_RUN.STATUS='failed'`, `REPORT_JSON=NULL`).

검증이 실제로 작동한 기록이 DB 에 있다. RUN_ID 1 은 에이전트가 스냅샷의 `open_instruction_count=1` 을 보고 `WF:1` 이라는 ID 를 **추측해** 썼고, `get_center_detail` 을 호출하지 않아 그 ID 가 `evidence` 에 없었다 → "조회되지 않은 근거 ID 'WF:1'" 로 폐기. 이후 시스템 프롬프트에 "ID 를 추측해 쓰면 보고서 전체가 폐기된다" 문구를 추가했다. RUN_ID 2 는 `get_center_detail` 을 호출했는데도 같은 메시지로 폐기됐는데, 원인은 검증 코드의 형식 검사 `len(eid) > 4` 가 4글자 ID `WF:1` 을 거부한 결함이었다(접두어 뒤 1글자 이상으로 수정). RUN_ID 3 과 6 은 통과했다. 두 실패는 "모델의 추측" 과 "검증 코드 결함" 이라는 서로 다른 원인이며, 도구 로그에 `get_center_detail` 이 있는지로 구분한다.

### 2.3 승인 계층 — 쓰기는 사람의 버튼에서만

`db.approve_draft(draft_id, title, body, priority)` 가 유일하게 초안을 지시로 바꾸는 함수이고, 이것을 부르는 곳은 `views/hq.py` 의 승인 버튼뿐이다. 승인은 `대기` 상태에서 한 번만 되고, 생성된 `WF_INSTRUCTION` 은 `SOURCE='에이전트초안'` 으로 사람이 직접 보낸 지시(`'본사직접'`)와 구분된다. 반려하면 `반려` 로 남고 다음 사이클에 다시 대상이 된다(3장).

모든 INSERT/UPDATE 는 `db.py` 함수로만 하고, `views/` 에 SQL 문자열이 없다는 것을 `tests/test_ui.py::test_views_have_no_sql` 이 검사한다.

### 2.4 계층 사이의 데이터 흐름 (한 사이클)

```
1. db.start_run(mode='agent'|'rules')                          → AGENT_RUN(running)
2. rules.detect_anomalies()        주의·위험 전부, 위험 우선 정렬
3. monitor.plan_targets()          대기 초안 지문과 비교 → 대상 / 건너뜀_중복 / 건너뜀_한도
   db.add_alert() 전수 기록                                     → MON_ALERT
4. 대상 0 → finish_run('skipped')
5. agent.analyze(question, scope=대상)  또는  rules.rule_report(대상)
6. db.add_draft(초안, fingerprint=대상 센터 지문, source)         → WF_DRAFT(대기)
7. db.finish_run('success', report, tool_log, model, num_turns, cost_usd, duration_ms)
예외: AgentUnavailable → finish_run('failed', 한국어 사유), 초안 0. 다른 예외도 failed 기록 후 스케줄러 루프 유지.
```

---

## 3. 지문(fingerprint) 설계 — 같은 질문을 두 번 하지 않기

### 3.1 문제

스케줄러는 5분마다 돈다. 센터 상태는 대부분 그대로다. 아무 장치가 없으면 같은 센터에 대해 같은 진단을 5분마다 Claude 에게 물어 비용만 쓰고 초안만 쌓인다.

### 3.2 정의

```python
key = "|".join([center_code, status, ",".join(sorted(bottlenecks)), ",".join(sorted(open_event_ids))])
fingerprint = sha1(key)[:16]
```

- **넣는 것**: 등급, 병목 집합, 미처리 이벤트 ID 집합. "이 센터가 지금 처리해야 할 상태" 를 이루는 **이산값**.
- **안 넣는 것**: 정시출고율 같은 연속값, 시각. 소수점이 흔들리면 매 사이클 지문이 바뀌어 중복 방지가 무력화된다.

실제 값 예(기준일 2026-10-07 샘플): C003 `a11bacae9ac1eee7`, C005 `6866d46e8285dad3`, C006 `e306ad94c827ec75`.

### 3.3 사이클에서의 사용

| 상황 | 처리 |
|---|---|
| `WF_DRAFT.status='대기'` 에 같은 센터·같은 지문이 있음 | `건너뜀_중복`. 사람이 아직 안 본 초안이 있고 상황도 그대로면 다시 물을 이유가 없다 |
| 대기 초안은 있지만 지문이 다름 | 대상. 상황이 바뀌었으니 새 진단이 필요하다 |
| 초안이 반려됨(`반려`) | `대기` 가 아니므로 다음 사이클에 다시 대상 |
| 초안이 승인됨 → 지시 → 센터가 이벤트 처리 | 이벤트 ID 집합이 바뀌어 지문이 바뀜 → 새 상태로 판정 |
| 대상이 `max_centers_per_cycle`(2) 을 넘음 | 초과분 `건너뜀_한도`. 다음 사이클에서 백로그 처리 |

실측: 실제 사이클(RUN_ID=6) 직전에는 규칙 기반 실행이 만든 대기 초안 3건이 있어 `plan_targets` 가 세 센터 모두 `건너뜀_중복` 을 돌려줬다(= Claude 호출 없이 `skipped` 로 끝났을 것). 초안 3건을 반려한 뒤 돌리자 C003·C005 가 대상, C006 이 `건너뜀_한도` 가 됐고 Claude 가 한 번 호출됐다.

### 3.4 왜 MON_ALERT 에 전부 남기나

건너뛴 센터도 `MON_ALERT` 에 `TARGETED=0`, `SKIP_REASON` 과 함께 기록한다. 콘솔에서 "왜 이번엔 안 물어봤는지" 가 보여야 운영자가 지문 로직을 믿을 수 있다.

### 3.5 구현하지 않은 것

STEP0_PLAN 5.2 ⑤의 `cooldown_minutes`(같은 지문으로 최근 `success` 한 실행이 있으면 건너뜀)는 구현하지 않았다. 에이전트가 초안을 0건 내고 끝나는 경우에만 의미가 있는데, 현재 프롬프트는 대상 센터마다 초안 1건을 요구하므로 발생 빈도가 낮다고 봤다. 필요해지면 `plan_targets` 에 조건 하나를 더하면 된다.

---

## 4. Claude Agent SDK 를 선택한 이유

### 4.1 후보

| 선택지 | 장점 | 단점 |
|---|---|---|
| Anthropic Messages API 직접 호출 | 가장 단순, 의존성 적음 | **API 키 필요**(CLAUDE.md 금지). 도구 루프·재시도·구조화 출력을 직접 구현 |
| LangChain 등 프레임워크 | 도구·메모리 추상화 | 역시 API 키. 추상화 계층이 두꺼워 교육용으로 "무슨 일이 일어나는지" 가 가려짐 |
| **Claude Agent SDK** (`claude-agent-sdk`) | **Claude Code 로그인 재사용**, 도구 루프 내장, `output_format=json_schema`, 인프로세스 MCP 서버(`create_sdk_mcp_server`)로 파이썬 함수를 바로 도구로 노출 | CLI 서브프로세스를 띄우므로 호출당 수십 초. 옵션 필드가 버전마다 달라 `inspect` 로 확인 필요 |

### 4.2 결정 요인

1. **API 키 없음.** 교육 과정 참가자가 각자 키를 발급·관리·노출하는 위험을 없앤다. SDK 는 `.venv` 안의 번들 CLI(`claude_agent_sdk/_bundled/claude.exe`)를 띄우고 Claude Code 로그인 정보를 그대로 쓴다. 실제 실행 로그에서 확인했다.
2. **도구 제어가 선언적이다.** `tools=[]` 로 내장 도구를 빼고 `allowed_tools` 로 우리 MCP 서버만 허용하는 것이 옵션 두 줄이다. 직접 API 를 쓰면 "모델이 요청한 도구를 실행하지 않는다" 를 루프 코드로 지켜야 한다.
3. **구조화 출력이 내장.** `output_format={"type":"json_schema","schema":...}` 와 `ResultMessage.structured_output` 으로 pydantic 모델을 바로 받는다.
4. **인프로세스 도구.** `@tool` 핸들러가 같은 프로세스에서 `db.py`/`rules.py` 를 호출하므로, 핸들러가 `evidence[eid] = center_code` 와 `tool_log` 를 채우고 검증이 그 구조를 그대로 본다. 별도 MCP 프로세스였다면 이 "도구 결과에 실제로 있던 ID" 추적이 훨씬 번거로웠다.
5. **멀티턴 채팅.** `resume=session_id` 로 콘솔의 운영 질의가 이어진다.

### 4.3 SDK 를 쓰며 확인한 것 (설치본 0.2.164, `inspect` 기준)

- `ClaudeAgentOptions` 에 CLAUDE.md 예시의 필드(`tools`, `allowed_tools`, `mcp_servers`, `permission_mode`, `setting_sources`, `max_turns`, `model`, `effort`, `output_format`, `resume`, `cwd`)가 모두 있다.
- `ResultMessage` 에 `model` 필드가 **없다**. `MODEL` 컬럼은 `model_usage` 의 키 또는 옵션의 `model` 값으로 채운다(실측 `claude-sonnet-5-5`).
- `total_cost_usd`, `num_turns`, `duration_ms`, `session_id`, `structured_output`, `subtype` 을 그대로 `AGENT_RUN` 에 적는다.
- 동기 코드(Streamlit·CLI)에서는 `asyncio.run(asyncio.wait_for(coro, timeout))` 로 감싼다. 타임아웃은 `config.toml [agent].timeout_seconds`(180).
- Claude Code 세션 안에서 다시 SDK 를 띄우면 중첩 세션으로 인식되므로 `CLAUDECODE`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_ENTRYPOINT` 를 비우고 실행해야 한다. 일반 터미널에서는 불필요.

### 4.4 실측 (RUN_ID=6, 2026-10-07)

| 항목 | 값 |
|---|---|
| 대상 | C003(위험), C005(주의) — 2센터 |
| 도구 호출 | snapshot → pending_drafts → detail C003 → detail C005 (4회) |
| NUM_TURNS / DURATION_MS / COST_USD | 6 / 35568 / 1.1174454 |
| 초안 | 2건(C003 긴급, C005 보통), 보고 항목 4개 모두 포함, 근거 ID 전부 검증 통과 |

1센터 단독 실행(RUN_ID=3)은 5턴 / 23509ms / 0.244792 USD 였다. 비용은 조회한 이벤트·지시 수와 응답 길이에 비례해 크게 변한다.

---

## 5. [보완] 기준 — PRD 에 없는 것을 어떻게 정했나

원칙: **PRD 에 있는 값은 그대로, 없는 값은 PRD 의 다른 값에서 유도하고 새 기준은 최소로.** 전부 `config.toml [rules]` 에서 읽고 화면·README 에 출처를 적는다.

| 기준 | 값 | 출처 | 유도 근거 |
|---|---|---|---|
| 정시출고율 목표 | 97% | PRD 4.1.1 | — |
| 병목 입고/보관/출고 | 미입고>3 / 상태이상>0 / 미납>5 | PRD 4.2.3 | — |
| 병목 피킹/패킹 | 항상 False | PRD 에 없음 | **기준을 새로 만들지 않음.** 화면에는 단계가 보이되 빨강이 되지 않는다 |
| 히트맵 | 0 녹 / 1~3 노 / 4+ 적 | PRD 4.1.3 | — |
| **위험** | 정시출고율 < 95% 또는 미처리 상태이상 ≥ 4건 | [보완] | 95 = 목표 97 에서 2pt 미달(샘플 7일 분산 고려). 4건 = PRD 히트맵 "적" 경계 **재사용** |
| **주의** | 위험 아님 + (정시출고율 < 97% 또는 미처리 이벤트 ≥ 1건) | [보완] | PRD 목표값과 "미처리 건 확인" 페르소나 관심사에서 직접 유도 |
| **데이터 없음** | 기준일 KPI 행 없음 | [보완] | 회색 카드, 탐지 대상 아님 |
| 이상 발생 센터 수 | 주의 + 위험 센터 수 | [보완] | PRD 4.1.1 에 용어 정의 없음 |
| 이벤트 지연색 | 기한 이내 녹 / 초과 24h 미만 노 / 이상 적 | [보완] | PRD 4.2.5 "지연 기준" 만 있고 값 없음. `due_at` = 발생시각 + 유형별 SLA(샘플 24h/48h) |
| "미납" | 출고 미이행 수량 | [보완] | LLM 이 금융 미수금으로 오독하지 않도록 시스템 프롬프트에 명시 |
| 초안 우선순위 | 위험 → 높음/긴급, 주의 → 보통/높음 | [보완] | 프롬프트 가이드. 검증하지 않음(실측: C003 긴급, C005 보통) |
| 사이클당 센터 수 | 2 | [보완] | 비용 상한 |
| 타임아웃 / 최대 턴 | 180초 / 12턴 | [보완] / CLAUDE.md | 실측 6턴·36초로 여유 있음 |

컬럼 보완: PRD 7.1 은 테이블 **이름만** 정의하므로 12개 테이블의 컬럼은 화면(PRD 4장)이 요구하는 수치를 역산해 정했다. `EXC_CENTER_EVENT.DUE_AT`(지연색), `WF_INSTRUCTION.SOURCE`(사람/에이전트초안 구분)는 PRD 에 없는 보완 컬럼이다. 추가 테이블 3개(`AGENT_RUN`, `MON_ALERT`, `WF_DRAFT`)는 CLAUDE.md 가 허용한 것이다.

STEP0 설계와 실제 구현의 차이(이름 수준): `MON_ALERT` 는 `DISPOSITION` 한 컬럼 대신 `TARGETED`+`SKIP_REASON`, `WF_DRAFT` 는 `REVIEWED_AT/REVIEWER_NOTE` 대신 `DECIDED_AT/DECIDED_BY/DECISION_NOTE`, `AGENT_RUN` 에 `DETECTED_JSON` 추가, config 키는 `timeout_seconds`/`interval_seconds`/`use_agent`. 의미는 같다.

---

## 6. 알려진 제약

### 6.1 실행 환경

- **Python 3.13.13** 으로 개발·테스트했다. 요구 조건은 3.11+ 이며, 버전 의존 코드는 `tomllib`(3.11+) 뿐이다(시간대는 `datetime.timezone` 으로 고정 KST). 3.11/3.12 에서는 돌려보지 않았다.
- **Windows 전용으로 검증.** 경로·`PYTHONIOENCODING=utf-8`·Git Bash 기준. Mac/Linux 에서는 `.venv/bin/python` 으로 바꾸면 될 것으로 보이나 실행하지 않았다.
- **단일 사용자·로컬 SQLite**(PRD 6장). 스케줄러와 Streamlit 이 같은 DB 파일을 동시에 쓰면 SQLite 잠금 대기가 생길 수 있다. 트랜잭션이 모두 짧아 실제로 문제가 될 가능성은 낮다고 보지만, 둘을 동시에 띄운 상태로 검증하지는 않았다.
- **스케줄러는 포그라운드 프로세스.** 서비스·데몬 등록, 재시작, 로그 파일은 없다.

### 6.2 에이전트

- **호출당 수십 초**(실측 23~46초). SDK 가 CLI 서브프로세스를 띄우기 때문이며, 화면 버튼을 누르면 그동안 스피너만 보인다.
- **비용이 변동적**(실측 0.24~1.24 USD/호출). `max_centers_per_cycle`·`max_turns`·지문 dedupe 로 상한을 두지만 "하루 얼마" 는 상태 변화 횟수에 달려 있어 추정만 가능하다.
- **검증 실패 = 전체 폐기.** 근거 ID 하나만 틀려도 그 실행의 보고서 전체가 버려진다(RUN_ID 1·2). 부분 채택은 일부러 하지 않았다 — "검증 통과한 보고서만 화면에 보인다" 는 불변식이 더 중요하다고 봤다. 대신 폐기된 실행도 도구 로그·턴·비용은 남겨 원인을 볼 수 있다.
- **등급 외 수치는 검증하지 않는다.** `observation` 에 적힌 숫자(예: "정시출고율 92.4%")가 도구 결과와 일치하는지는 확인하지 않는다. 근거 ID 가 실제 조회된 것임은 보장하지만, 그 ID 가 가리키는 값을 LLM 이 옮겨 적을 때 생기는 오류는 사람이 초안을 읽으며 잡아야 한다.
- **프롬프트 주입 방어는 문구 수준.** 도구 결과의 메모·제목이 지시문이 아니라고 프롬프트와 `note` 에 명시했을 뿐, 악의적 메모로 실험하지는 않았다. 다만 LLM 이 할 수 있는 일이 "읽기 + JSON 작성" 뿐이므로 주입이 성공해도 발송·승인·삭제는 일어날 수 없다.
- **`MODEL` 컬럼은 요청값.** `ResultMessage` 에 모델 필드가 없어 `model_usage` 의 키 또는 옵션의 `model` 을 적는다. 서버가 다른 모델로 응답해도 알 수 없다.
- **채팅 세션은 브라우저 세션 한정.** `session_id` 를 `st.session_state` 에만 두므로 새로고침하면 새 대화가 된다.
- **쿨다운 미구현.** 3.5 참고.

### 6.3 데이터·화면

- **샘플 데이터뿐.** `data_kind=sample` 이고 summary 첫 줄에 "샘플 데이터" 가 붙는다. 실데이터 연동(ETL·스키마 매핑)은 범위 밖.
- **피킹·패킹 병목은 영원히 녹색.** PRD 에 기준이 없어 만들지 않았다. 실데이터에서 피킹 단계 문제를 보려면 기준을 PRD 에 먼저 추가해야 한다.
- **외부 알림 없음.** 초안이 쌓여도 이메일/Slack 으로 알리지 않는다(PRD 11장). 사이드바 경고 배지가 전부다.
- **화면 테스트는 렌더링·버튼 존재·클릭 결과까지.** `streamlit.testing.v1.AppTest` 로 세 화면이 예외 없이 그려지고 승인 버튼이 `WF_INSTRUCTION` 을 만드는지 확인했다. 브라우저 확인은 별도로 1회 했고, 그때 다크 테마에서 배경색 카드·행의 글자가 보이지 않는 결함을 발견해 `color:#1f1f1f` 를 지정했다. AppTest 는 이런 시각 결함을 잡지 못한다.

### 6.4 테스트가 덮지 않는 것

- 실제 Claude 호출(테스트는 항상 `analyze` 를 monkeypatch). 실제 호출은 수동 1회(RUN_ID=6)뿐이다.
- 스케줄러의 반복 루프(`--once` 경로만 테스트).
- SQLite 동시 접근, 긴 운영(며칠)에서의 `MON_ALERT`/`AGENT_RUN` 누적.
