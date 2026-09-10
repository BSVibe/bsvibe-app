# E2E — 런별 LLM 토큰 계측 + 런어웨이 상한 (게이트 1)

`LlmClient.chat` 은 usage 토큰을 이미 캡처하고 `ChatResponse` 가 실어 날랐지만
`LoopTurn` 에서 버려졌다. 이제 런별 집계로 남고(가시성), 런어웨이는 상한으로 막는다.
BYO-key 라 비용은 사용자 프로바이더 청구 — 목표는 런어웨이 보호 + 파운더 가시성.

## 검증

- [x] 마이그레이션 `run_token_usage`: execution_runs 에 usage_prompt/completion_tokens
      (bigint, default 0). fresh-PG upgrade/downgrade 양방향 실증
- [x] 배선: LoopTurn 토큰 필드 → loop_llm.complete 캐리(getattr 방어) → _drive_loop 누적
- [x] 계측: 두 턴(100+40, 70+10) 런 → run.usage = 170/50
- [x] 상한: cap=1000, 단일 거대 턴(9000+2000) → run_token_cap_reached Decision(파운더 알림),
      루프가 2번째 턴을 안 태움(len(calls)==1)
- [x] uncapped: agent_max_run_tokens=0 → 상한 없음, 계측만
- [x] run detail 응답에 usage 두 필드 노출
- [x] god-file: 상한 로직을 token_budget.py 로 추출(_drive_loop 600 LOC 유지)
- [x] 프로브 PG 전체 스위트 · mypy(584) · import-linter(5 kept) · ruff
- [ ] 배포 후 prod: 실제 런이 돌면 execution_runs.usage_* 가 채워지는지 관측
      (다음 자연 런에서 — 파운더가 발주하는 런)

## 후속 (별도 PR — 데이터 본 뒤 정책 결정)

무료 플랜 워크스페이스별 토큰 예산(일/월) · 하드 차단 vs 경고 · 상한값 튜닝
(현 기본 2M 은 데이터 없이 정한 안전 천장). 감사 §Ⅴ 게이트 1.
