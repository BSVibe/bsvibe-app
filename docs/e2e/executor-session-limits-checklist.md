# E2E — Claude Code 세션 안에서 한도가 걸린다 (#1104, #1114)

2026-09-30 실측: 런 `92b76fba` 의 Claude Code 태스크는 `done` 으로 정상 종료했다(입력 3,055,575 ·
출력 30,336). 그 **뒤에야** 누적이 `agent_max_run_tokens`(2M)를 넘어 `run_token_cap_reached` 로
멈췄다. 상한이 폭주를 막은 게 아니라 끝난 일을 버렸다. 원인은 세 가지였다.

- **단위:** `input + cache_creation + cache_read` 를 같은 무게로 합산했다. Claude Code 는 턴마다
  대화 전체를 캐시에서 다시 읽으니, 비용(캐시 읽기 ≈ 1/10)은 그대로인데 이 합만 초선형으로 불었다.
- **시점:** 사용량을 세션이 끝난 뒤의 `result` 이벤트에서만 읽었다.
- **세션 내 한도 없음:** `--max-turns` 가 없어서 2시간 타임아웃이 유일한 한도였다.

## 실측한 CLI 동작 (2.1.286, 2026-10-01, haiku 1회)

- 한 assistant 메시지의 content block 마다 **같은 `message.id` 로 같은 `usage` 가 반복된다**.
  합산하면 두 배로 센다. 그 `output_tokens` 는 스트리밍 중간값이라 실제보다 작다(4 vs 최종 233).
- 캐시 쓰기는 **1시간 TTL**(`ephemeral_1h_input_tokens`)이다. 단가는 입력의 2배(5분 TTL 은 1.25배).
- `--max-turns` 에 닿으면 `result/error_max_turns`, `is_error: true`, **exit 1**. 그대로 두면
  태스크 실패 → 어댑터가 재시도 → 한도가 막은 일을 새 세션이 반복한다.

## 바뀐 것

- **단위(#1104):** `_weighted_prompt` — 입력 + ⌈캐시 읽기×0.1⌉ + ⌈5분 쓰기×1.25 + 1시간 쓰기×2⌉.
  런 미터·런 상한·워크스페이스 월 예산 모두 이 "입력 환산 토큰"으로 센다(claude_code 만).
- **시점(#1104):** 어댑터가 에이전트 턴을 디스패치할 때 런에 **남은** 예산(`cap − 사용량`, 최소 1)을
  `token_budget` 으로 보낸다. 워커는 assistant 메시지마다 `message.id` 로 중복 제거한 누적을 재고,
  예산에 닿으면 프로세스 그룹을 kill 한다. 터미널 청크는 **정상 종료**(재시도 방지)이며 누적 사용량을
  싣는다. 그러면 드라이브 루프의 기존 상한 검사가 `run_token_cap_reached` Decision 을 낸다.
- **턴 한도(#1114):** `executor_agent_max_turns`(기본 60, `0` = 플래그 없음)가 에이전트 세션에
  `--max-turns` 로 붙는다. `error_max_turns` + exit 1 은 정상 종료로 읽고, 검증이 결과를 판정한다.
- 재전송(#965) 경로도 같은 한도를 싣는다. 채팅 턴(frame / judge)에는 아무것도 붙지 않는다.

## 검증 (로컬)

- [x] RED → GREEN
  - `tests/executors/worker/test_claude_code_session_limits.py`
    - 가중치(1시간 쓰기 2배 · TTL 분해 없으면 1.25배)
    - `--max-turns` 유무, `error_max_turns` + exit 1 → 정상 종료
    - 예산 도달 → kill · 이후 줄 미소비 · 같은 id 블록은 한 번만 · 예산 없으면 kill 없음
  - `tests/dispatch/test_executor_session_limits_reach_the_worker.py`
    - 어댑터: 남은 예산, 상한 도달 시 1, 상한 0 이면 없음, 채팅 턴엔 없음, 재전송에도 같은 값
    - 페이로드: dispatch / redispatch 둘 다 싣고, 없으면 키를 빼고
    - 워커: 페이로드 → 실행기 컨텍스트
  - 기존 `test_worker_token_usage.py` 의 동일 가중 합 단언을 새 단위로 갱신(옛 명제 자체가 #1104 의 결함)
- [x] 전선 절단 8곳, 각각 해당 테스트만 빨개진다(15개 수집 유지)
  - 어댑터 dispatch 인자 · 어댑터 재전송 인자 · 페이로드 키 · 워커 컨텍스트
  - kill 판정 · max-turns 정상화 · `--max-turns` 플래그 · message.id 중복 제거
- [x] 전체 7218 passed · import-linter 6/6 · ruff · mypy

## 배포 후 (prod)

⚠️ **워커 변경이 있다.** 백엔드 autodeploy 만으로는 반쪽이다. 페이로드에 키가 실려도 옛 워커는
무시한다(무해하지만 효과도 없다).
- `main` 에서 `git pull` 하고 `launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` 실행
- **프로세스 시작 시각**으로 재시작을 확인한다

- [ ] 디스패치 페이로드에 `token_budget`·`max_turns` 가 실린다(워커 `task_received` 직후 컨텍스트, 또는 Redis 스트림 항목)
- [ ] 정상 에이전트 런의 `executor_tasks.usage_prompt_tokens` 가 새 단위로 기록된다. 같은 규모 과제의 이전 값보다 크게 작아야 한다(캐시 읽기 비중만큼)
- [ ] 턴 한도: 큰 과제에서 `claude_code_max_turns_reached` 가 찍히면 태스크는 `done` 이고 재시도(`executor_adapter_chat_retry`)가 없다
- [ ] 예산 kill: 낮춘 상한으로 런을 한 번 돌린다. 확인할 것:
  - `claude_code_token_budget_exhausted` 가 찍힌다
  - 태스크 `done`
  - 이어서 `run_token_cap_reached` Decision 이 생긴다
  - 그 시점의 런 누적이 상한을 크게 넘지 않는다
  - ⚠️ 상한 변경과 prod 런은 형님 OK 를 받은 뒤 진행한다

## 남은 틈

- **원시 캐시 분해는 아직 DB 에 없다.** 행에는 가중 합만 남는다. 원시 값은 결과 이벤트에 있지만
  컬럼이 없다. 사후 비용 재계산이 필요하면 `executor_tasks` 에 컬럼을 추가해야 한다(#1104 의 세 번째 항목, 미완).
- 세션 중 누적의 `output_tokens` 는 낮게 잡힌다(스트리밍 중간값). 입력이 압도적인 에이전트 세션에선
  무시할 만하지만, kill 은 실제보다 약간 늦을 수 있다.
- `token_budget` 은 커밋된 런 행을 읽는다. 직전 턴이 아직 회계되지 않았다면 예산이 그만큼 넉넉하다.
  상한의 기록 주체는 여전히 백엔드 검사다.
- **opencode 는 이 키들을 무시한다.** #1000 이후 opencode 도 BSVibe 도구로 에이전트 턴을 받으므로, opencode 런에는 세션 내 한도가 없다. codex 와 opencode 는 캐시를 여전히 동일 가중으로 센다. 그래서 같은 상한이 실행기마다 다른 양을 뜻한다.
- #1114 의 나머지 절반(라운드마다 전체 대화를 다시 넣는 대신 BSVibe 소유 요약)은 #1103 과 함께 간다.
