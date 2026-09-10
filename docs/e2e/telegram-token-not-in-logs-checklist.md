# E2E — 텔레그램 봇 토큰이 로그/에러 표면에 안 샌다 (H4)

봇 토큰이 URL 경로(`/bot<token>/method`)에 있어 httpx `HTTPStatusError` 메시지가
그걸 담고, `PluginRunner._call` 의 `str(exc)` 가 로그·`PluginRunError`·런
`ActionResult`·502 본문 4곳으로 퍼뜨렸다.

## 검증

- [x] 텔레그램 클라이언트가 HTTP 에러를 `TelegramApiError`(토큰 scrub)로 재발생
- [x] `from None` — 원본 httpx 예외(토큰 보유)를 체인에서 끊음(`__suppress_context__`)
      → `exc_info=True` 트레이스백 유출 차단
- [x] send/delete 양쪽 raise_for_status 경유(`_raise_for_status`)
- [x] 절단 실증: scrub 무력화 시 유출 테스트 3개 빨강, 복원 시 초록
- [x] 통합: 러너 `dispatch_action` 통과 후 `PluginRunError` 메시지에 토큰 없음
- [x] 전체 6725 passed, ruff/format/mypy(583)/import-linter(5 kept) 통과
- [ ] 배포 후: 텔레그램 전송 실패가 실제로 나면 로그/알림에 토큰 없음 관측

## 범위 노트

러너 경계 generic redaction 은 커넥터 격리 import 계약을 깨서 채택 안 함
(`plugin.* → 러너 → backend.shared` 금지). 소스 수리가 실제 유출(텔레그램)을
origin 에서 완전 차단하므로 충분. github/slack/notion 은 헤더 인증이라 URL 에
시크릿 없음, trello 는 raise_for_status 미사용 — 다른 커넥터는 유출 경로 없음(감사 확인).
