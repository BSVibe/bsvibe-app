# E2E — Connector "Connect with X" busy feedback

기능: OAuth 커넥터 연결 버튼이 클릭 즉시 "Connecting…" + 스피너 + `aria-busy`로
작업 중임을 표시해, start 왕복 → provider 리다이렉트 구간이 멈춘 것으로 오인되지 않게 함.

## 체크리스트

- [x] 유닛: 클릭 후 start 진행 중 라벨이 `Connecting…`, `aria-busy=true`, disabled (vitest)
- [x] 유닛: start 완료 후 `onRedirect`로 authorize_url 이동 (기존 테스트 유지)
- [x] 유닛: connected 시 버튼 없음 / needsReauth 시 Reconnect (기존 테스트 유지)
- [x] biome / tsc / vitest(664) / next build 통과
- [ ] 라이브(prod): Settings → Connectors에서 Connect 클릭 시 즉시 스피너+Connecting… 노출
      → 왕복 중 재클릭 유발 없이 provider 인증 화면으로 이동 (founder 확인)
- [ ] 라이브: prefers-reduced-motion 환경에서 스피너 회전 정지(정적 표시)
