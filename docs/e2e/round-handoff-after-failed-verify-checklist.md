# E2E — 검증에 실패한 라운드가 다음 라운드에 자기 보고를 넘긴다 (#1114 요약 절반)

#1114 는 "라운드 경계마다 지금까지의 대화를 통째로 새 세션에 다시 넣는다"고 적었다. 코드를 읽으니
반대쪽 틈이었다.

- 다시 넣는 것은 **시드 컨텍스트**(시스템 메시지 · 지시 · 지식)다. 새 세션이니 피할 수 없다
- 검증 실패 시 루프가 붙이는 것은 `user: Verification FAILED …` **하나뿐**이었다.
  라운드 자신의 최종 보고(`turn.content`)는 버려졌고, 이미 바꾼 파일도 알려주지 않았다
- 그래서 다음 세션(`--resume` 없음)은 앞 세션이 무엇을 찾고 했는지 모른 채 "고쳐라"만 읽고
  탐색을 처음부터 다시 했다

## 바뀐 것

- `_loop_turn.failed_round_messages` — 실패한 라운드가 다음 라운드에 넘기는 것을 대화 순서대로 만든다
  - `assistant`: 그 라운드의 보고
  - `user`: 실패 내용 + **"Files this run has changed so far:"** 목록 + 다시 하라는 지시
- 실패가 여전히 **마지막** 메시지다(`test_verification_feedback` 의 명제 유지)
- 모두 평문이라 어느 실행기든 읽는다. 이어감은 BSVibe 의 것이다
- `_drive_loop.py` 는 583 → 581 줄(600 상한)

## 검증 (로컬)

- [x] RED → GREEN — `tests/execution/test_round_handoff_after_failed_verify.py`
  - 다음 라운드가 앞 라운드의 보고를 본다
  - 보고가 실패보다 앞이고, 실패가 마지막이다
  - 실패 메시지에 바뀐 파일 줄이 있다
- [x] 전선 절단 2곳(보고 · 바뀐 파일), 각각 해당 테스트만 빨개진다
  - ⚠️ 처음 절단은 같은 문자열 `written_paths=written_paths,` 의 **첫 번째** 출현(토큰 상한 호출)을
    바꿔서 초록이었다. 새 호출 자리를 정확히 끊자 빨개졌다
- [x] 기존 `test_verification_feedback` · `test_drive_loop_stuck_review_hint` · `test_h2a_decomposition` 통과

## 배포 후 (prod)

워커 변경 없음. 백엔드 autodeploy 만으로 반영된다.

- [ ] 검증이 한 번 실패하는 런이 생기면(자연 발생을 기다린다), 두 번째 실행기 태스크의 프롬프트에
  첫 라운드의 보고(`assistant:`)와 `Files this run has changed so far:` 가 있다
  - 프롬프트는 `executor_tasks` 행에 남는다 — prod DB 조회가 필요해 형님 쪽에서 확인
  - 일부러 실패를 만드는 prod 런은 형님 OK 를 받은 뒤

## 남은 틈

- 라운드의 보고는 에이전트가 쓴 요약이다. 탐색 내역(읽은 파일)은 담지 않는다
- 시드 컨텍스트 재주입 자체는 그대로다 — 새 세션에는 필요하다
