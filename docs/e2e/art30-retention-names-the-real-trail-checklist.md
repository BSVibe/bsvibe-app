# E2E — Art. 30 보존 기록이 실재하는 감사 흔적을 가리킨다

`GET /api/v1/workspace/processing-record` 의 `retention` 이 **producer 없는
`audit_events`** 를 주어로 "1년 보관"을 약속하고 있었다. 실제 감사 흔적은
`audit_outbox` 이고 보존은 워크스페이스별 `audit_retention_days`(NULL = forever
가 기본)다.

**이 체크리스트가 걷는 것**: 유닛은 픽스처 워크스페이스를 쓴다. 여기서는
**prod 의 진짜 행**으로 같은 문장을 확인한다 — prod 3/3 워크스페이스가 NULL 이라
기본 분기가 실제로 서빙되는 분기다.

## 배포 전 (로컬)

- [ ] `uv run pytest tests/api/test_v1_workspace_compliance.py -q` 전부 통과
- [ ] 절단 실증: `_audit_retention_sentence` 를 옛 문자열
      (`"Retained 1 year for security incident review."`)로 되돌리면 새 테스트
      **둘 다** 빨개진다 (개수까지 확인 — 컴파일 되는 절단이어야 한다)

## 배포 후 (prod)

- [ ] prod 컨테이너가 이 커밋으로 갱신됐다 (`docker inspect` 시작 시각 + 커밋)
- [ ] `GET /api/v1/workspace/processing-record` 응답의 `retention` 에
      **`audit_events` 키가 없다** (음성 대조군)
- [ ] 같은 응답에 `audit_outbox` 키가 있고 그 문장이 `audit_retention_days` 와
      `forever` 를 말한다 (prod 워크스페이스는 NULL 이므로 기본 분기)
- [ ] DB 대조: `SELECT audit_retention_days FROM workspaces` 가 전부 NULL 이어서
      응답의 문장과 **실제 컬럼이 일치**한다
- [ ] `SELECT count(*) FROM audit_events` = 0 이고 `audit_outbox` 는 증가 중
      (양성 대조군 — 문장이 가리키는 테이블이 살아 있는 쪽이다)
