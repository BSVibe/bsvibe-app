# E2E — 토큰 상한에 멈춘 런에 질문과 다음 걸음이 있다 (#1105)

2026-09-30 실측, 런 `92b76fba` 의 체크포인트 `fb38eb66`: `kind=run_token_cap_reached`, `question=""`,
`options=null`, `actions=null`. `checkpoints_resolve(action_key=retry)` → "has no one-click actions".
형님이 할 수 있는 건 폐기뿐이었다. 10-02 에도 같은 모양의 빈 Decision 이 09-30 의 런 `58a7426d` 에
남아 있어 손으로 정리했다.

#1104 이후 세션 안 예산 kill 이 이 Decision 을 **더 자주** 만든다 — 그래서 지금 고친다.

## 바뀐 것

- **질문:** "이 작업이 끝나기 전에 토큰 예산을 다 썼어요 — 예산을 더 주고 이어갈까요, 접을까요?" (ko/en)
- **액션:** `retry` "예산 늘려 계속" · `discard` "폐기". `ship` 은 없다 — 검증에 닿지 못한 작업이다
- **retry 는 예산을 준다** (`checkpoint_resolution._grant_more_token_budget`)
  - 사용량은 런에 누적되므로 그냥 재개하면 첫 턴에 같은 상한에 다시 걸린다
  - Decision 이 멈출 때 기록한 사용량 + 상한 하나 = 새 상한 → `run.payload["token_cap_granted"]`
- **두 집행 지점이 그 값을 읽는다** (`domain.run_token_cap.effective_run_token_cap`)
  - 루프의 턴 후 검사(`token_budget`)
  - 어댑터가 워커에 보내는 세션 안 예산(`ExecutorAdapter._session_limits`, #1104)
  - 배포 전역 상한이 0(꺼짐)이면 그대로 꺼짐 — 준 예산은 상한을 늘릴 뿐 만들지 않는다

## 검증 (로컬)

- [x] RED → GREEN
  - `tests/workflow/test_token_cap_checkpoint_is_actionable.py`
    - 질문이 비지 않는다(ko/en) · 액션은 retry/discard
    - resolve retry → `token_cap_granted == 사용량 + 상한`, 런 OPEN
    - 루프 검사가 준 상한을 따른다 · 준 상한을 넘으면 다시 멈춘다(대조군)
  - `tests/dispatch/test_executor_session_limits_reach_the_worker.py::test_a_granted_ceiling_reaches_the_session_budget`
- [x] 전선 절단 3곳(루프 · 어댑터 · resolve), 각각 해당 테스트 1개만 빨강 / 14 수집
  - ⚠️ 첫 절단 실행은 "no tests ran" 이었다 — zsh 가 `$T` 를 단어로 나누지 않아 pytest 가 경로 하나를
    받았다. 수집 개수로 잡았다
- [x] `tests/workflow/application/` 전체 통과

## 배포 후 (prod)

워커 변경 없음 — 예산 값은 어댑터(워커 컨테이너의 드라이브 루프)가 계산해 페이로드로 보낸다.

- [ ] 다음에 `run_token_cap_reached` 가 생기면 PWA·MCP 체크포인트에 질문과 두 버튼이 보인다
- [ ] "예산 늘려 계속" 후 런이 OPEN → 재개되고, 다음 실행기 태스크가 `token_budget ≈ 상한` 으로 간다
  (워커 로그는 예산을 찍지 않는다 — kill 이 일어나면 `claude_code_token_budget_exhausted` 에 찍힌다)
  - 일부러 상한을 낮춘 prod 런은 형님 OK 를 받은 뒤

## 남은 틈

- 준 예산은 고정 폭(상한 하나)이다. 금액을 고르는 UI 는 없다
- #1074(`ambiguous_model_account` 빈 체크포인트)는 같은 계열이지만 별개다 — 체크포인트 종류마다 질문·액션
  계약을 강제하는 가드는 아직 없다
