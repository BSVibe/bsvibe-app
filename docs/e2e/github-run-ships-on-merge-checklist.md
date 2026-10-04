# E2E — GitHub 배송 런은 PR 이 머지될 때 shipped 가 된다 · 종결은 종결이다 (#1109)

GitHub 배송 런은 PR 이 **열리는 순간** `shipped` 가 됐고, 그 PR 에 충돌이 나면 merge watch 가
`shipped → open` 으로 되돌려 에이전트가 충돌을 풀게 했다. `TERMINAL_RUN_STATUSES` 를 기준으로 도는 리퍼·
동시 런 상한·대시보드와 모순이었다. 형님 결정(2026-10-04): **shipped 는 머지 후에만.**

## 규칙

| 상황 | 런 |
|---|---|
| PR 열림 + merge watch 행 있음 | `review_ready` 에서 대기, `payload.awaiting_merge = {repo, pr_number}` |
| PR 열림 + watch 행 없음(auto-merge 꺼짐 · enqueue 실패) | 지금처럼 열릴 때 `shipped` — 머지를 볼 수단이 없다 |
| watch 가 머지를 봄(직접 squash · 이미 머지됨) | `shipped`, 표식 제거 |
| PR 이 머지 없이 닫힘 | `cancelled`, 표식 제거 |
| watch 포기(CI 실패 · 기한 · 저장소 끊김) | `review_ready` 유지 + `merge_watch_stalled` Decision 에 **폐기** 추가 |
| 머지 대기 런 | 동시 런 상한에서 **제외**(PR 3개가 새 요청을 막지 않게) |

## 바뀐 것

- **전이 표** (`run_status.is_allowed_move`): `shipped` 는 나가는 전이 없음, `failed`·`cancelled` 는 재시도(→ `open`)만
- **배송 해소** (`run_delivery_resolution`): 살아 있는 watch 행이 있으면 대기 표식, 없으면 기존대로 ship. 리뷰 Decision
  경로도 같다(→ `review_ready` + 표식)
- **merge watch** (`merge_watch_worker`): 새 주입 콜백 `PrConcluded(run_id, merged, pr_number)` 를 세 결말(직접 squash ·
  이미 머지됨 · 머지 없이 닫힘)에서 커밋 후 부른다. 실패해도 틱은 죽지 않는다
- **콜백 구현** (`merge_watch_runtime.build_merge_watch_pr_concluded`): `move_run_status` 로 `shipped`/`cancelled`. 옛 규칙으로
  이미 shipped 인 런은 표가 거절해 그대로다
- **동시 런 상한** (`run_caps.count_held_runs`): `awaiting_merge` 표식이 있는 `review_ready` 를 뺀다
- **stall Decision**: 액션 `acknowledge` + `discard`. 자유 텍스트 답은 기록만 하고 런을 재개하지 않는다(재개하면 끝난 작업을 다시 돈다)
- 옛 규칙을 적은 문서(배송 해소 모듈 docstring · stall 콜백 docstring · `StallEscalate`)를 고쳤다

## 검증 (로컬)

- [x] RED → GREEN
  - `tests/glue/test_one_run_state_machine.py` — 전이 표(shipped 에서 나가기 4종 거절 · failed/cancelled 는 재시도만)
  - `tests/glue/test_github_run_ships_on_merge.py` — B 배송 3 · C 결말 3 · D 상한 2 · E stall 3
  - `tests/workflow/infrastructure/test_merge_watch_worker.py` — 결말 3종 보고 · CI 대기 중엔 보고 없음(대조군)
- [x] 기존 명제 재진술 2건
  - `test_stalled_offers_acknowledge_only` → `..._acknowledge_or_discard`: 형님 결정으로 명제 자체가 바뀌었다(대기 중인 런은 놓아줄 수 있다)
  - `test_free_text_reply_still_resumes_a_paused_running_run` — stall 은 런을 RUNNING 에 두지 않으므로
  만들 수 없는 픽스처였다. RUNNING 런을 실제로 파킹하는 `ask_user_question` 으로 같은 명제를 잰다
- [x] 전선 절단 9곳, 각각 빨강 / 69 수집
  - ⚠️ 첫 squash 절단은 `if` 본문을 비워 **컴파일 오류**("1 error")였다 — 무효로 보고 `pass` 로 다시 끊었다
- [x] ruff · mypy · import-linter 6/6
- [x] 전체 스위트: 6619 passed, 2 failed → 1건은 위 재진술. 다른 1건 `test_every_captured_line_is_json` 은 단독·새 테스트와
  함께 돌려도 통과했고, 실패한 런의 로그 파일은 뒤의 실행에 덮여 원인을 못 봤다 — **재현 못 함**으로 기록(#950/#967 계열). CI 의 전체 실행으로 재확인
- [x] 옛 배송 테스트(`test_run_delivery_resolution_github_bound` 등)는 watch 행을 시드하지 않아 "watch 없으면 열릴 때 ship"
  경로를 그대로 잰다 — 전부 통과

## 배포 후 (prod)

워커 변경 없음 — 바뀐 것은 백엔드·워커 컨테이너(autodeploy). prod 는 `BSVIBE_GITHUB_AUTO_MERGE_ENABLED=true`.

- [ ] 다음 GitHub 배송 런: PR 이 열린 뒤 `review_ready` + `awaiting_merge`, 동시 런 상한에 안 잡힌다
- [ ] 그 PR 이 머지되면 `merge_watch_pr_concluded merged=true` → 런 `shipped`
- [ ] 충돌이 나면 `review_ready → open` 재작업이 되고(더는 shipped 에서 나가지 않는다), 다시 검증 후 `review_ready`
- [ ] 배포 시점에 이미 shipped 인 런의 watch 가 충돌을 만나면 재작업 전이가 표에 막힌다
  (`run_status_move_not_allowed`) — watch 는 재시도하다 escalate 한다. 옛 런에 한정된 전환기 현상

## 남은 틈

- watch 가 포기(`failed`)한 뒤 사람이 직접 머지하면 아무도 보지 못한다 — 런은 `review_ready` 로 남는다(상한에는 안 잡힘).
  Decision 의 폐기 또는 별도 머지 감지가 필요하다
- `shipped` 알림은 여전히 검증 시점에 나간다 — #1111
- 머지 대기 중에도 런 스코프 MCP 토큰은 살아 있다(`load_run` 은 종결 런만 거절). 실행기 세션은 끝났으므로 실사용 경로는 없다
