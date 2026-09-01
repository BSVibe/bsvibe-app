# `GET /deliverables/{id}/report` ↔ `bsvibe_deliverables_report` — 검증 체크리스트

> 2026-09-01. 인수인계가 "규칙을 workflow 로 옮겨라"로 확정해둔 건. **재보니 규칙은
> 이미 거기 있었다** — 진짜 블로커는 다른 것이었고, 처방도 달랐다.

## 인수인계가 틀린 지점

*"report builder reaches backend.connectors via the narrative; same move needed"*

규칙은 **이미** `workflow.application.report_narrative` 에 있었다. 진짜 블로커는
`_narrative.py:215` 의 **함수 레벨 import** — 헤더 grep 이 놓치는 자리다:

```
_narrative → report_narrative → loop_llm → agent_loop → connectors/extensions
                              → runtime.account_resolution → router
                              → dispatch.adapter → executors
```

**에이전트 엔진 전체**가 딸려와 4개 컨텍스트에 닿는다. "옮기기"로는 안 닫힌다.

## 실험이 처방을 바꿨다

`report_narrative.py` 를 손대는 대신 — **호출자가 아예 임포트하지 않으면 된다.**
프로브로 확인했다:

```python
# _references / _schemas 만 임포트한 프로브
backend.mcp is not allowed to import backend.api:   ← 경로 엣지 하나뿐
```

깊은 위반 4개가 전부 없다. ⇒ **`report_narrative.py` 는 한 줄도 안 고쳤다.**

## 새 형태

| | |
|---|---|
| `workflow/serialization/deliverable_views.py` | 응답 모양 (← `_schemas.py`, `git mv`) |
| `workflow/application/deliverable_references.py` | 참고지식 유도 (← `_references.py`) |
| `workflow/application/deliverable_narrative.py` | 캐시 규칙 + **`NarrativeGenerator` Protocol** (← `_narrative.py`) |
| `workflow/application/deliverable_report.py` | **`build_deliverable_report`** (← proof.py 본문) |
| `api/v1/deliverables/_narrative_generator.py` | **런타임** — `ReportNarrativeService` 래핑 |
| `api/v1/deliverables/proof.py` | 얇은 어댑터 |

`lint-imports` **5 kept · 0 broken — 새 예외 0건.**

## ⚠️ retract 와 정반대인 점 — 부재 처리

`report_narrative_for` 는 **캐시 우선**이다. LLM 은 캐시 미스 + verified 일 때만 돈다.

| | 부재 시 |
|---|---|
| retract (**쓰기**) | **거절** — 침묵하면 일어나지 않은 회수를 성공으로 보고하게 된다 |
| report (**읽기**) | **degrade** — 산출물·검증·참고지식은 그대로, 새 문장만 없다 |

같은 주입 패턴이지만 부재의 의미가 반대다. 테스트로 둘 다 고정했다.

## 가드

| 테스트 | 명제 |
|---|---|
| `…report_composes_the_same_proof_the_browser_gets` | `verified` 는 **PASSED 검증 실재**로만 참 (행 존재로 추론 안 함) |
| `…report_without_a_generator_still_returns_the_proof` | 생성기 부재 = degrade, 거절 아님 |
| `…report_other_workspace_is_not_found` | 교차 워크스페이스 = 미상과 구분 불가 |
| `test_build_server_installs_the_injected_runtimes_into_every_tool_context` | **배선** (retract + narrative 둘 다) |

⚠️ 배선 가드는 **전선을 끊어 빨강을 실증했다** (`assert [None] == [sentinel]`).
⚠️ not-found 는 메시지까지 못박았다 — `raises(ToolError)` 는 *"unknown tool"* 로도
만족돼서 툴이 없을 때 **거짓 초록**이 된다.

## 체크리스트

- [ ] `uv run pytest --cov=backend --cov=plugin --cov-fail-under=80` → green
- [ ] `uv run lint-imports` → **5 kept, 0 broken**
- [ ] `uv run mypy backend/` → clean
- [ ] `_KNOWN_GAPS` 2 → **1** (양방향 fail-closed — 세지 말고 읽어라)
- [ ] 배포 후 prod: 툴 등록 + **주입 도달**(narrative 가 null 이 아닌지)
