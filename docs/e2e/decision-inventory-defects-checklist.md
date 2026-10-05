# E2E — #1074 인벤토리에서 찾은 Decision 결함 (프레임 · 조회 · 번들 충돌)

#1074 를 고치며 코드가 만들 수 있는 Decision 을 전부 훑었고, 범위 밖 결함 넷이 나왔다. 셋을 고치고 하나는 남긴다.

## 1. 프레임 모델이 없을 때 런이 Decision 없이 `running` 에 멈춘다 — 고침

- `agent_worker` 의 `FrameModelUnresolvedError` 분기는 **act** 호출자로 계정을 해석했다. act 쪽이 해석되면(활성 계정이 하나 ·
  act 룰 · 기본 계정) Decision 이 안 쓰이는데도 런을 `running` 으로 파킹하고 claim 을 지웠다 — 아무도 다시 집지 않는다
- 이제 Decision 이 안 쓰였으면 파킹하지 않고 예외를 다시 던진다 → 기존 drive-failure 경로(재시도 상한 → `run_drive_failed`
  체크포인트: 다시 시도 / 폐기)
- 테스트: `tests/workflow/test_framing_failure_reaches_the_founder.py::test_an_unrouted_frame_never_parks_the_run_without_a_decision`
  (RED: `running` + Decision 0개). 계정이 아예 없는 기존 경우(`no_model_account` 로 파킹)는 그대로

## 2. merge watch 의 계정 조회가 Decision 을 쓴다 — 고침

- `merge_watch_client_box` 는 체크아웃을 가진 워커를 찾으려고 같은 해석기를 불렀고, 실패하면 해석기가 Decision 을 썼다.
  그 세션이 커밋을 안 해서 롤백됐을 뿐이다
- `resolve_workspace_model_account(..., record_decision=False)` — 조회는 런을 멈추지 않는다. 호출 지점이 플래그를 넘기는지도 따로 단언

## 3. 번들 게시 충돌이 곧 shipped 될 런에 retry/discard 를 단다 — 고침

- main 머지는 이미 성공했고 보관본 게시만 갈라졌다. `merge_conflict_review`(retry/discard)로 올렸는데, 런은 바로 shipped 가 되고
  전이 표가 두 액션을 조용히 거절한다
- 새 종류 `product_bundle_conflict`: 질문(ko/en) + "확인했어요", 보고(`_REPORT_DECISION_KINDS`)라 자유 텍스트 답이 재개하지 않는다.
  폰 본문(`product_bundle_publish_conflict`)도 추가
- #1074 가드가 새 종류를 바로 요구했다 — 레지스트리에서 빼면 빨강

## 4. `human_review_required` 는 만드는 곳이 없다 — 남김

- prod DB 에 그 종류의 pending Decision 이 남아 있을 수 있다. 매핑을 지우면 그 행이 빈 질문이 된다. 그대로 둔다

## 검증 (로컬)

- [x] RED → GREEN 세 건
- [x] 전선 절단 6곳(재던지기 · 플래그 · 호출 지점 플래그 · 레지스트리 · 액션 · 보고 분류), 각각 빨강
  - ⚠️ 호출 지점 절단은 처음에 바늘이 0번 맞았다(ruff 가 호출을 한 줄로 접었다) — 그 초록은 무효, 실제 텍스트로 다시 끊어 빨강
- [x] ruff · mypy · import-linter

## 배포 후 (prod)

워커 변경 없음.

- [ ] 자연 발생 대기 — 세 경로 모두 드물다. 로그 `agent_worker_frame_model_unresolved_without_decision` 이 찍히면 그 런에 곧
  `run_drive_failed` 체크포인트가 생기는지 본다
