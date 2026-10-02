# E2E — 한 단위의 일은 한 런에서, 쪼갤 때는 실행기 중립 인계를 남긴다 (#1103)

2026-09-30 실측: 이슈 #1102 → 런 `92b76fba`. 프레이머가 본문의 번호 목록을 따라
`frame.steps` 를 **둘 다 `implement`** 인 두 스텝으로 만들었다.
1. 경합을 재현하는 테스트 작성 + 빨강 확인
2. `transition()` 을 compare-and-set 으로 수정

첫 런의 Claude Code 는 프롬프트 끝의 "This run is ONE step of that request" 를 따라 테스트만
쓰고 끝냈다(입력 3,055,575 토큰). 산출물은 빨간 테스트만 있는 PR 이었고, 두 번째 세션은 같은
탐색을 처음부터 다시 해야 했다.

## 바뀐 것

- **이웃한 같은 단계는 합친다** (`frame._merge_adjacent_same_stage`)
  - 쪼갬이 사는 것은 라우팅 하나다. 같은 단계는 같은 모델로 가므로 쪼개서 얻는 게 없다
  - 합칠 때 모든 intent 를 순서대로 남긴다(`1. … 2. …`)
  - 다른 단계가 사이에 있으면 합치지 않는다. 그 스텝은 앞 단계의 산출물이 필요해서 뒤에 있다
- **프레이머 프롬프트:** "A failing test and the fix that makes it pass are ONE step, never two —
  a numbered list in the request is not a reason to split." 단계 이름이 다르게 쪼개는 경우
  (`test` → `implement`)는 결정적으로 합칠 수 없어서 여기서 막는다
- **한 스텝짜리 계획에는 "ONE step" 스코프 줄을 붙이지 않는다** (`_loop_context._intent_directive`).
  계획 전체가 그 한 스텝이다
- **진짜 쪼갬의 인계** (`handoff.compose_step_handoff`). 다음 스텝의 `prior_output_text` 앞에
  다음을 붙인다. 모두 평문이라 어느 실행기든 읽는다(이어감은 BSVibe 의 것 — `--resume` 아님)
  - 앞 스텝 deliverable 의 `summary`(앞 세션이 찾고 한 일)
  - 건드린 파일 목록
  - 최신 검증 결과
  - 그 뒤에 기존대로 산출물 파일 내용

## 검증 (로컬)

- [x] RED → GREEN
  - `tests/glue/test_frame_keeps_one_unit_in_one_run.py`
    - 92b76fba 의 쪼갬 → 한 스텝, 두 intent 가 순서대로 남는다
    - 이웃하지 않은 반복은 유지 · 단계가 갈리는 쪼갬은 유지(대조군)
    - 프롬프트에 TDD 한 단위 규칙
    - 한 스텝 계획엔 "ONE step" 없음 · 진짜 쪼갬엔 있음(대조군)
  - `tests/execution/test_handoff_chain.py` — 인계에 보고 · 건드린 파일 · 검증 결과
- [x] 기존 명제 재진술 2건
  - `test_a_stage_may_repeat_within_the_cap`: 상한은 개수만 본다는 명제는 그대로, 반복을 이웃하지 않게
  - `test_spawn_inlines_none_when_the_output_is_unreadable`: 읽을 수 없는 파일의 **내용**은 지어내지
    않는다. 파일 이름은 이제 인계에 실린다
- [x] 전선 절단 6곳, 각각 해당 테스트만 빨개진다
  - 병합 호출 · 프롬프트 문장 · 한 스텝 분기
  - 인계의 보고 · 건드린 파일 · 검증 결과
- [x] ruff · ruff format · mypy · import-linter 6/6

## 배포 후 (prod)

워커 변경 없음. 백엔드 autodeploy 만으로 반영된다.

- [x] 형님 워크스페이스의 단계 룰(`design` / `implement`)이 그대로인 상태에서, 번호 목록으로
  "테스트를 쓰고 → 고친다"를 적은 요청 하나가 **런 하나**로 끝난다
  - **2026-10-02 실측 (런 `66d4b25e`, `588d986`):** 런 하나가 `implement` 룰로 sonnet 에 라우팅,
    산출물에 테스트(`tests/glue/test_join_intents_skips_blank.py`)와 수정(`frame.py`)이 함께,
    검증 passed, `handoff_next_step_spawned` 없음. 입력 86,264 토큰(92b76fba 의 테스트 절반 3,055,575).
    관측 후 폐기
  - 미확인: `payload.frame.steps` 와 프롬프트 문구 자체는 MCP 로 노출되지 않아 결과로만 판정했다
  - `payload.frame.steps` 가 한 개거나, 두 개여도 단계가 서로 다르다
  - 에이전트 프롬프트(첫 user 메시지)에 "This run is ONE step" 이 없다
  - 산출물에 테스트와 수정이 함께 있다
  - ⚠️ prod 런을 일부러 여는 행동이므로 형님 OK 를 받은 뒤 진행한다
- [ ] 진짜 쪼갬(`design` → `implement`)이 생기면, 두 번째 런의 `payload.prior_output_text` 에
  `## What the prior step reported` · `## Files the prior step touched` ·
  `## The prior step's verification` 이 있다

## 남은 틈

- **라운드 경계** (#1114 의 요약 절반)는 별도 변경이 다룬다 — `docs/e2e/round-handoff-after-failed-verify-checklist.md`
- 앞 스텝의 `summary` 는 에이전트가 쓴 보고라 탐색 내역(읽은 파일)을 다 담지는 않는다.
  실행기 중립적으로 탐색 내역을 남기려면 work 도구 호출(`file_read` · `file_list`) 로그에서
  뽑아야 한다
- 단계 이름이 다른 TDD 쪼갬(`test` → `implement`)은 프롬프트로만 막는다
