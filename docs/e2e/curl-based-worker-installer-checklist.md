# E2E — curl 기반 워커 설치 (게이트 2)

프론트 가이드는 `bsvibe login && bsvibe-worker register && bsvibe-worker run` 을
보여줬지만 그 CLI 를 **얻는 curl 스텝이 없었다**(가이드가 한 스텝 늦게 시작). 낯선
사용자가 워커를 붙일 물리 경로가 끊겨 있었다. GitHub Actions runner 식으로 복원.

## 검증

- [x] `GET /install-worker.sh` 무인증, `text/x-shellscript` 로 스크립트 반환
- [x] 스크립트: `#!/bin/sh`, `sh -n` 통과, uv 설치→리포 clone(PUBLIC, 무인증)→
      uv sync→`~/.bsvibe/bin` 에 bsvibe/bsvibe-worker 래퍼(멱등)
- [x] 시크릿 미포함(등록은 이후 `bsvibe login` 으로 대화형 인증)
- [x] 프론트: 설치 카드에 **1단계 curl 부트스트랩** + 2단계 register/run 두 CopyField
- [x] curl 스텝이 THIS 배포 backend 를 가리킴(`api.bsvibe.dev/install-worker.sh`)
- [x] Dockerfile `COPY backend/` 가 정적 .sh 포함 → 배포본에서 라우트가 읽음
- [x] 백엔드 라우트 테스트 2 · 프론트 테스트 10(부트스트랩 포함) · PWA 780 · biome · tsc
- [ ] 배포 후 prod: `curl -fsSL https://api.bsvibe.dev/install-worker.sh` 가 스크립트 반환
- [ ] (선택) 실 호스트에서 스크립트 실행 → bsvibe-worker register 성공까지 라이브

## 범위

CLI 획득 경로만 복원. Supabase 이메일 가입(리포 밖 설정)과 기본 계정 자동지정
(별도 백엔드 수리)은 게이트 2 의 다른 조각 — 후속.
