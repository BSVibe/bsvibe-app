# E2E — Connected connector Reconnect / Switch-to-OAuth

기능: 연결된(is_active) oauth-capable 커넥터 카드에 "Reconnect with X" 액션을
상시 노출. 두 가지를 UI로 가능하게 함:
- PAT-backed github 바인딩 → OAuth로 in-place 마이그레이션 (delivery_config repo 보존)
- 정상 OAuth 바인딩 → 자격증명 회전/복구 (revoke 없이)

이전엔 backend가 needs_reauth로 표시할 때만 재연결 버튼이 떠서, 정상 회전/마이그레이션
경로가 없었음.

## 체크리스트

- [x] 유닛: 정상 OAuth-backed github → 초록 Connected pill + Reconnect 버튼, 중복 identity 라인 없음
- [x] 유닛: PAT-backed github(oauth 라벨 없음) → Reconnect 버튼 노출 (gh-e2e 마이그레이션)
- [x] 유닛: needs_reauth github → Reconnect (기존 유지)
- [x] 유닛: revoked(inactive) → Reconnect/Connect 버튼 없음
- [x] 유닛: 정상 slack 등 비-github oauth 커넥터도 Reconnect 일관 노출
- [x] biome / tsc / vitest(666) / next build 통과
- [ ] 라이브(prod): admin's workspace의 gh-e2e 커넥터 카드에서 Reconnect with GitHub 클릭
      → OAuth(@blas1n) → 기존 바인딩 재사용(repo=blas1n/bsvibe-gh-e2e 보존) + OAuth 토큰 바인딩
- [ ] 라이브: 재연결 후 gh-e2e delivery push 성공 + #496 스크럽으로 .git/config 토큰 잔존 0
