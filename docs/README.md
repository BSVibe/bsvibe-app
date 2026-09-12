# bsvibe-app — 문서 인덱스

**모든 현행 문서는 이 레포 안에 있다.** 원격/클라우드 세션은 clone 하나만 갖기
때문에, 레포 밖 문서는 그 세션에게 존재하지 않는 것과 같다. 근거와 개정 경위는
[architecture/INVARIANTS.md](./architecture/INVARIANTS.md) 서두를 보라.

## 여기서 시작

| 파일 | 역할 |
|---|---|
| **[STATUS.md](./STATUS.md)** | **Master Status SoT.** 전체 상태·아키텍처·구현 표·운영. 모든 세션은 여기서 시작한다. Living — in-place 갱신, 새 status 파일 만들지 말 것. |
| **[HANDOFF.md](./HANDOFF.md)** | 가장 최근 세션 인수인계. **최신 1건만 유지** — 새 세션이 끝나면 이 파일을 덮어쓰고, **덮어쓰기 전에 직전 판을 [Notion 아카이브](https://app.notion.com/p/3d9edf4af708814aa578ddb83643bbca) 로 옮긴다.** |
| **[audit/multiuser-readiness-2026-09-10.md](./audit/multiuser-readiness-2026-09-10.md)** | 다중 사용자 준비도 감사. 게이트 0~4 로드맵이 여기 있다. 열린 작업은 GitHub 이슈. |
| **[이슈](https://github.com/BSVibe/bsvibe-app/issues)** | **열린 작업은 전부 여기.** 게이트별 `gate-1`~`gate-4` · 리포 밖은 `founder-action` · 설계 트랙은 `open-track`. 문서 안의 백로그 목록은 낡는다. |

## 계층

- **[architecture/](./architecture/)** — 변하면 안 되는 것.
  - [INVARIANTS.md](./architecture/INVARIANTS.md) — 아키텍처 불변식. 문자열 엣지 가드.
  - [verification-declaration-contract.md](./architecture/verification-declaration-contract.md) · [github-auto-merge.md](./architecture/github-auto-merge.md)
  - [strategy-synthesis.md](./architecture/strategy-synthesis.md) — 단일제품 피벗 종합 (Strategy SoT).
  - [ux-design.md](./architecture/ux-design.md) — 4 surface UX (Direct / Brief / Decisions / Inside).
  - [workflow-backend.md](./architecture/workflow-backend.md) — Receive → Frame → Agent loop → ε 백엔드 설계.
  - [worktree-workspace.md](./architecture/worktree-workspace.md) — 워크트리 기반 워크스페이스 spec.
- **[design/](./design/)** — 살아있는 설계 계약. 코드가 SoT 로 인용하는 문서.
  - [client-attach-execution.md](./design/client-attach-execution.md) — client_attach + in-place verify.
  - [production-verification.md](./design/production-verification.md) — 증명을 머지 너머로.
  - [execution-mode-parity.md](./design/execution-mode-parity.md) — server_sandbox ↔ client_attach 파리티.
  - [tool-surface.md](./design/tool-surface.md) — 툴 표면, 결정 지점 제거.
  - [pipeline-removal-routing.md](./design/pipeline-removal-routing.md) — `pipeline` 제거 + 라우팅 재설계 (열린 트랙).
- **[audit/](./audit/)** — 현행 감사.
- **[e2e/](./e2e/)** — E2E 체크리스트. 기능마다 한 장.

## 정리 원칙

- **현행이면 레포 안.** 상태·인수인계·설계·감사·E2E 전부.
- **할 일이면 GitHub 이슈.** 문서 안의 `- [ ]` 는 그 문서 범위의 검증 항목일 때만.
- **종결된 이력은 `BSVibe/internal-docs`.** 코드가 인용할 때는 `internal-docs:<파일명>`
  으로 표기한다 — 열리지 않는 경로를 경로처럼 쓰지 않는다.
- **과거 인수인계는 [Notion 아카이브](https://app.notion.com/p/3d9edf4af708814aa578ddb83643bbca).** 날짜별 세션 로그는 사람이 읽는
  것이고, 에이전트가 코딩 중 볼 것은 `STATUS.md` 하나로 수렴한다. 2026-09-07~09-10
  5건이 거기 있다.
- **회고 자산은 `~/.claude/skills/`** — 함정·교훈은 스킬 시스템에.
