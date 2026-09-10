# E2E — unclaimed 설치 claim 은 소유 증명을 요구한다 (H2)

전역 `list_unclaimed` 로 아무 워크스페이스가 남의 Sentry 설치를 목록에서 보고
먼저 claim → 그 워크스페이스에 남의 OAuth 토큰이 바인딩됐다(선착순 레이스).

## 검증

- [x] claim 이 `(provider, installation_ref)` 매칭 — ref 는 자기 Sentry org 에서만 보임
- [x] 오답 ref → 404(없는 것과 구분 불가, 오라클 없음), 원래 행은 진짜 소유자를 위해 생존
- [x] 전역 목록 제거: REST `GET /unclaimed` 삭제 + MCP `bsvibe_connectors_list_unclaimed` 툴 삭제(파리티)
- [x] MCP `claim_install` input: `unclaimed_id` → `provider`+`installation_ref`
- [x] 교차 테넌트 방어 테스트(store·service·REST·MCP 4계층): 오답 ref 로 바인딩 0
- [x] 단일 사용: 같은 ref 재claim → 404
- [x] `test_scope_audit` 낡은 allowlist 항목 제거(가드 정직성 유지), 새 POST claim 은 정상 스코프
- [x] 전체 스위트 6718 passed, ruff/format/mypy 통과
- [ ] 배포 후: prod 는 unclaimed 0행·Sentry 미설정이라 라이브 영향 없음 — 회귀 관측만

## UX 변경 (Sentry 실제 활성화 시)

전역 목록이 없으므로 파운더는 자기 Sentry org(Settings → Integrations)에서
installation id 를 읽어 claim 에 제시한다. PWA 의 Sentry 연결 화면이 "목록에서 선택"
대신 "설치 ID 입력"으로 바뀌어야 한다(현재 미구현·미사용이라 후속).
