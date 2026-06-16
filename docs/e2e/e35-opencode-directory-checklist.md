# E35 E2E Checklist — opencode adapter ?directory= wire

- [x] U1 — 새 test `test_session_create_passes_workspace_dir_as_directory_query` GREEN
- [x] U2 — 새 test `test_session_create_omits_directory_when_workspace_dir_missing` GREEN
- [x] U3 — 기존 16개 opencode test 회귀 없음 (16/16 PASS)
- [x] LV1 — 라이브 opencode 1.15.12 `POST /session?directory=/tmp` → response `directory=/private/tmp` (probe-verified pre-fix)
- [ ] PR1 — CI 통과 (사전 존재한 audit_events 2건 제외)
- [ ] DOG1 — 머지 후 dogfood: 새 run 의 `artifact_changed_files_captured` > 0
- [ ] DOG2 — 호스트 source repo `git status` clean 유지 (에이전트 leak 차단)
- [ ] DOG3 — opencode session 응답의 `directory` 가 per-task workspace 와 일치
