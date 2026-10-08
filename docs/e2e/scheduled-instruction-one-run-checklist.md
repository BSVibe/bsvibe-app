# E2E — 스케줄 지시는 런 하나로 끝난다 (#1079)

실측 2026-09-29: BStockReport 주간 리포트("명령 하나 실행 → 결과 발행")가 2단계 계획으로 쪼개졌다.
1단계 `cc68f583` — 셸 13회, 게이트 통과, 산출물 0 → `review_ready` (상한 슬롯 차지). 2단계 `a65412ad` — 같은 지시를 처음부터
(`has_prior_output=false`) 다시 실행해 발행. 토큰·시간 2배, review_ready 2개.

## 바뀐 것

- `agent_worker._split_vocabulary_for(session, request)` — SCHEDULE 트리거 요청이면 단계 어휘 없음(→ 프레이머가 쪼개지 않는다),
  그 외는 지금처럼 워크스페이스 규칙의 단계
- 프레임 호출부가 워크스페이스 단위가 아니라 요청 단위로 묻는다

## 검증 (로컬)

- [x] RED → GREEN — `tests/glue/test_a_scheduled_instruction_is_one_run.py` (스케줄 → [], direct·webhook → 단계 유지, 호출부 배선)
- [x] 전선 절단 2곳(스케줄 판정 · 호출부) 각각 빨강
- [x] tests/glue · tests/workflow 1776 passed · ruff · mypy · lint-imports

## 배포 후 (prod)

- [ ] 다음 BStockReport 주간 런(일요일 00:30 KST)이 런 1개, `handoff_next_step_spawned` 없음

## 남은 틈

- 스케줄이 아닌 런의 "산출물 없는 선행 단계"는 그대로 review_ready 에 남는다(설계 단계는 산출물이 없는 게 정상일 수 있다) — 수용 기준 2번째 줄은 이 변경으로 스케줄에 대해서만 해소
