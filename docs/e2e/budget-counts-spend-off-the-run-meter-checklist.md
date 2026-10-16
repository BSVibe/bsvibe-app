# 워크스페이스 예산은 런 미터 밖의 지출도 센다

`sum_workspace_tokens_since` 는 `execution_runs.usage_*` 만 합산했다. 워크스페이스 원장
(`unattributed_llm_usage`, #954)은 쓰기만 하고 아무도 안 읽었고, 늦은 실행기 보고(#928, #1158)는
태스크 행에만 남았다. 이제 늦은 보고는 같은 트랜잭션에서 원장 행도 남기고
(`site="executor_late_report"`, identity 에 `task_id` + `run_id`), 예산은 런 + 원장을 합산한다.

백엔드만 — 컨테이너 폴러가 자동 배포. 마이그레이션 없음.

## 유닛 / 배선 (로컬)

- [x] 런 없는 지출이 예산을 넘기면 → `/api/v1/messages` 429
- [x] 창(이번 달) 이전의 원장 지출은 세지 않는다 (대조군)
- [x] 다른 워크스페이스의 원장은 이 워크스페이스의 지출이 아니다 (교차 테넌트로 읽어 쿼리 자신의 술어를 잰다)
- [x] `/workers/result` 의 늦은 보고 → 형님의 다음 제출이 거절된다
- [x] 늦은 보고는 런과 함께 원장 행 **1개**, 재전송은 0개
- [x] 정시 보고는 원장 행을 만들지 않는다 (런 미터와 이중 계상 없음)
- [x] 전선 절단(합산 · 창 · 워크스페이스 술어 · 훅 호출 · API 훅) 각자 빨강 — SQLite **그리고** 일회용 PG(RLS, `bsvibe_app` 역할, `alembic upgrade head`) 양쪽

## prod (배포 후)

- [x] 설정 → 플랜에서 이번 달 토큰이 보인다 — 2026-10-16 형님 화면: 1,247,237 / 50,000,000
- [ ] 형님 DB 읽기(원장만 따로):
      `SELECT sum(usage_prompt_tokens+usage_completion_tokens) FROM unattributed_llm_usage WHERE workspace_id=… AND created_at >= date_trunc('month', now())`
- [ ] #1161 워커 재시작 후, 실행 중인 Claude Code 런을 턴 도중 취소 → 백엔드 로그
      `executor_task_late_usage_recorded`, 그 태스크의 `unattributed_llm_usage` 행(`site='executor_late_report'`)

## ⚠️ 배포 전에

창업자 워크스페이스의 ingest/bootstrap 지출이 이제 예산에 잡힌다. 이번 달 원장이 크면 배포 직후 첫 제출이
거절(`workspace_token_budget_reached`)될 수 있었다 — 위 화면 기준 여유가 충분했다.
