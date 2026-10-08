# E2E — 에이전트 턴은 CLI 내장 도구를 목록이 아니라 스위치로 끈다 (#1077)

BStockReport 주간 리포트가 08-17 ~ 08-31 세 주 연속 발행 0건. 런 `cb37cbcd`:
`aborted: the CLI's tools are not the ones BSVibe sanctioned (unsanctioned: ListAgents, ReportFindings, SendMessage)`.
에이전트 턴은 내장 도구를 **이름으로** 거부했다(`--disallowedTools <목록>`; 와일드카드 `"*"` 는 MCP 도구까지 죽인다).
CLI 업데이트가 새 내장 도구를 내면 init 가드가 거부 → **모든 에이전트 런 중단**. #814 는 이름 3개를 더했을 뿐이라 다음 릴리스에 재발한다.

## 실측 (2026-10-08, CLI 2.1.287, stdio MCP 서버 하나)

| 플래그 | init 이 노출한 도구 |
|---|---|
| `--tools ""` | `mcp__probe__probe_echo` 만 |
| `--tools "" --allowedTools mcp__probe__probe_echo` | `mcp__probe__probe_echo` 만 |

`--tools ""` = 내장 도구 집합 전체를 끈다(현재·미래), MCP 도구는 그대로.

## 바뀐 것

- `claude_code._NATIVE_TOOLS`(이름 목록) 삭제 → 에이전트 턴은 `--tools ""`
- init 가드(`_exposed_tools_are_ours`)는 그대로 — 보증은 여전히 노출 검사
- 알림 쪽: 실패한 런은 07-19(#596)부터 `failed` 알림, 현재 prefs 에서 failed=true · telegram 바인딩 있음. 8월에 왜 안 왔는지는 미확인(당시 바인딩 여부)

## 검증 (로컬)

- [x] RED → GREEN — `tests/executors/worker/test_builtins_are_switched_off_not_listed.py` (RED 2 + 가드 대조군)
- [x] 옛 명제("이름으로 거부")를 단언하던 테스트 재진술 · 목록 검사 파일 삭제
- [x] 전선 절단(플래그 제거) → 5 빨강
- [x] tests/executors 560 passed · ruff · mypy · lint-imports

## 배포 후 (prod) — ⚠️ 호스트 워커는 autodeploy 대상이 아니다

- [ ] 머지 후 워커 체크아웃 `git pull` → `launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` → 프로세스 시작 시각 확인
- [ ] 다음 에이전트 런의 워커 로그에 `claude_code_unsanctioned_tools` 없음, 런 정상 진행
