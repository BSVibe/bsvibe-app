# E2E — Art. 30 보존 기록이 실재하는 감사 흔적을 가리킨다

`GET /api/v1/workspace/processing-record` 의 `retention` 이 **producer 없는
`audit_events`** 를 주어로 "1년 보관"을 약속하고 있었다. 실제 감사 흔적은
`audit_outbox` 이고 보존은 워크스페이스별 `audit_retention_days`(NULL = forever
가 기본)다.

**이 체크리스트가 걷는 것**: 유닛은 픽스처 워크스페이스를 쓴다. 여기서는
**prod 의 진짜 행**으로 같은 문장을 확인한다 — prod 3/3 워크스페이스가 NULL 이라
기본 분기가 실제로 서빙되는 분기다.

## 배포 전 (로컬)

- [x] `uv run pytest tests/api/test_v1_workspace_compliance.py -q` → **7 passed**
- [x] 절단 실증 — 수정 **전** 상태에서 새 테스트 둘이 각각 옳은 이유로 빨갛고
      (`AssertionError: audit_events` · `KeyError: audit_outbox`) 나머지 5개는
      초록이었다. 자연 확보된 절단이라 되돌릴 필요가 없었다

## 배포 후 (prod)

**검증 방법**: HTTP 로 치려면 토큰이 필요해서, 대신 **배포된 컨테이너 안에서
그 코드를 직접 실행**했다 (`docker exec … /app/.venv/bin/python`, 읽기 전용).
정적 grep 보다 강한 증거다 — 실제 배포본이 렌더한 문서를 본 것이다.
⚠️ 컨테이너의 인터프리터는 `/app/.venv/bin/python` 이다. `python3` 로 치면
`fastapi` 가 없다고 나온다.

- [x] prod 컨테이너 갱신 — `StartedAt=2026-09-09T08:20:46Z`, 배포본 안에서
      신규 표현식 **1** · 옛 표현식 **0** (양성/음성 대조군)
- [x] 렌더된 `retention` 에 **`audit_events` 키 없음** (음성 대조군)
- [x] `audit_outbox` 키가 있고 문장이 `audit_retention_days` 와 `forever` 를
      말한다 — *"audit_retention_days is unset for this workspace — the default
      — so audit_outbox rows are retained forever…"*
- [x] DB 대조: `SELECT audit_retention_days, count(*) FROM workspaces GROUP BY 1`
      → **NULL 3건**. 렌더된 문장과 실제 컬럼이 일치한다
- [x] `audit_events` = **0행** · `audit_outbox` = **5,494행** (양성 대조군 —
      문장이 가리키는 쪽이 살아 있는 테이블이다)

## 후속

이 검증이 테이블 자체의 DROP 근거가 됐다 —
`20260909_drop_producerless_audit_events`.
