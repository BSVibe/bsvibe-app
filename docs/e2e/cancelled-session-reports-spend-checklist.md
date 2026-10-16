# #1158 — 취소된 실행기 세션도 쓴 토큰을 보고한다

워커만 바뀐다. **호스트 워커는 자동 배포가 아니다** — 머지 후 워커 체크아웃(`~/Works/bsvibe-app/main`)에서
`git pull` → `launchctl kickstart -k gui/501/com.bsvibe.worker-mac-mini-e2e` → **프로세스 시작 시각**이
바뀌었는지 본다. 백엔드는 새로 배울 것이 없다(#928 의 `failed` 행 usage 수용 경로가 받는다).

## 유닛 / 배선 (로컬)

- [x] 세션 도중 취소 → 세션 내 집계(가중 합 + 원시 분해)를 실은 `failed` 보고 1건
- [x] usage 가 생기기 전 취소 → POST 없음 (Lift E14 규칙 유지)
- [x] 워커 데드라인 → `failed` 보고에 집계가 실린다
- [x] 보고가 거절돼도 취소는 그대로 전파된다
- [x] 보낸 본문이 `WorkerResultBody`(`extra="forbid"`)로 파싱된다
- [x] 전선 절단: 실행기 집계 전달 · `_publish` · context 키 · 취소 보고 · 데드라인 usage — 각자 빨강, 매번 5개 수집

## prod (워커 재시작 후)

- [ ] 몇 턴 돈 Claude Code 런을 턴 도중 취소(`bsvibe_runs_cancel`). 워커 로그: `task_cancelled_by_backend`
      다음 `task_cancelled_usage_reported` 와 0 이 아닌 `usage_prompt_tokens`
- [ ] 백엔드 로그(`docker --context colima logs bsvibe-prod-backend-1`): 같은 `task_id` 로
      `executor_task_late_usage_recorded` — `executor_result_not_dispatched` 가 아니어야 한다
- [ ] 형님 DB 읽기: 그 `executor_tasks` 행이 `failed`, `output` 은 그대로, usage 컬럼은 0 이 아님
- [ ] 대조군: 첫 assistant 메시지 전에 취소한 런 → `task_cancelled_usage_reported` 없음

## 일부러 덮지 않은 것

- 런 미터(`execution_runs.usage_*`)에는 더하지 않는다 — #928 의 늦은 보고처럼 태스크 행에만 남긴다.
  런에 더하면 drive loop 의 절대값 ORM 쓰기와 경합한다. (워크스페이스 예산에는 #1162 의 원장으로 잡힌다)
- codex / opencode 는 세션 내 집계가 없다 — 취소된 세션은 여전히 아무것도 보고하지 않는다.
