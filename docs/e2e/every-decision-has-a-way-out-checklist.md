# E2E — 런은 형님이 답할 수 없는 Decision 에서 멈추지 않는다 (#1074)

2026-09-28 실측, 런 `cc68f583`: 스케줄 런이 `ambiguous_model_account` 에서 **30시간** 멈췄는데 아무도 몰랐다.
체크포인트는 `question=""` · `options=null` · `actions=null`. 알림도 없었다 — `resolve_workspace_model_account` 가
`create_decision`(형님을 부르는 유일한 자리)을 거치지 않고 날 `Decision(...)` 을 썼다. 형제 `no_model_account` 도 같은 모양.

#1105(`run_token_cap_reached`)가 같은 결함이었다. 종류를 하나씩 고치면 세 번째가 또 빠지므로, **모든 종류**에 대한 가드를 둔다.

## 바뀐 것

- 두 종류가 `create_decision` 을 거친다 → `needs_you` 알림이 나간다(폰 본문: `notifications.copy._NEEDS_YOU_REASON_BODY`)
- **`ambiguous_model_account`**
  - 질문: "이 작업을 어느 모델 계정으로 돌릴까요? 고른 계정이 워크스페이스 기본이 돼요."
  - 선택지 = 활성 계정 `라벨 (모델)`, `payload.account_choices` 가 선택지 → 계정 id 를 잇는다
  - 고르면 `checkpoint_resolution._make_picked_account_the_default` 가 워크스페이스 기본 계정으로 지정하고 런을 재개(재개 시 resolver 가 기본 계정을 쓴다).
    제안된 · 아직 있는 · 활성인 · 같은 워크스페이스 계정만 받는다
  - 액션: 폐기
- **`no_model_account`**: 질문 "이 작업을 돌릴 모델 계정이 없어요 — 계정을 연결한 뒤 다시 시도해주세요." · 액션 다시 시도 / 폐기
- 상수는 leaf 모듈 `workflow/domain/model_account_decision.py` 로 — MCP 가 닿는 `checkpoint_resolution` 이 런타임 그래프를 import 하지 않게

## 가드 (`tests/workflow/test_every_decision_has_a_way_out.py`)

- 코드가 만들 수 있는 모든 종류를 AST 로 모은다(리터럴 · 모듈 상수 · 같은 모듈 함수가 반환하는 리터럴). 자기 파라미터를 넘기는
  깔때기(`create_decision`, 루프의 `_create_decision`)는 호출 지점에서 센다. 풀 수 없는 인자는 `<unresolved …>` 로 빨개진다
- 모든 종류: 질문이 비지 않는다(ko/en) · 액션 또는 선택지가 있다 · 시스템 질문은 ko 와 en 이 **다르다**(빈 ko 는 en 으로 떨어져 비어 보이지 않으므로)
- 대조군: 스캔이 알려진 종류들을 실제로 찾는다

## 검증 (로컬)

- [x] RED → GREEN — 모호/없음 두 종류의 질문·선택지·알림·고르기→기본 지정+재개, 가드 41개
- [x] 전선 절단 6곳, 각각 빨강
  - ⚠️ "ko 질문을 비움" 절단이 처음엔 **초록**이었다 — `_question_text` 가 en 으로 떨어져 "비지 않음" 단언이 그 명제를 못 쟀다.
    "ko ≠ en" 으로 좁힌 뒤 빨강
- [x] ruff · mypy · import-linter 6/6

## 배포 후 (prod)

워커 변경 없음 — 백엔드·워커 컨테이너(autodeploy).

- [ ] 다음에 계정 해석이 막히면 텔레그램/PWA 에 질문과 계정 선택지가 뜬다(자연 발생 대기 — 지금 워크스페이스는 기본 계정이 있다)
- [ ] 고르면 워크스페이스 기본 계정이 바뀌고 런이 재개된다

## 남은 틈

- 런 상태는 여전히 `running`(Decision 에 파킹) — "입력 대기" 상태는 없다. 체크포인트 목록과 알림이 그 역할을 한다
- 인벤토리에서 따로 본 결함(이번 범위 밖):
  - `agent_worker` 프레임 실패 경로가 act 호출자로 해석해 Decision 없이 런을 `running` 에 파킹할 수 있다
  - `merge_watch_client_box` 의 계정 해석은 세션을 커밋하지 않아 Decision 이 롤백된다
  - `merge_conflict_review` (번들 게시 충돌)는 곧바로 shipped 되는 런 위에 생긴다
  - `human_review_required` 는 만드는 곳이 없는 죽은 매핑이다
