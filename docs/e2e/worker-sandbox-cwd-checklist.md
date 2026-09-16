# E2E — server_sandbox cwd 는 고정 디렉터리 하나

**대상 PR**: 워커의 per-task 임시 디렉터리 제거
**전제**: 호스트 워커는 **autodeploy 안 된다.** 배포 후 반드시
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 하고,
**프로세스 시작 시각**으로 새 코드인지 확인할 것.

> ⚠️ 이 PR 은 설정 키를 **`BSVIBE_WORKER_WORKSPACE_ROOT` → `BSVIBE_WORKER_SANDBOX_CWD`** 로 바꾼다.
> 2026-09-16 진단 실험 때 세 plist 에 넣어둔 옛 키는 **이 배포 전에 지워야 한다**(안 지우면
> `extra="ignore"` 라 조용히 무시되는 죽은 키로 남는다).

---

## 배포 전

- [ ] 세 워커 plist 에서 `BSVIBE_WORKER_WORKSPACE_ROOT` 제거
      (백업: 진단 세션 scratchpad `com.bsvibe.worker*.plist.bak`)
- [ ] `~/.claude/projects` 의 현재 항목 수를 **기록**한다 — 이게 베이스라인이다
      `find ~/.claude/projects -maxdepth 1 -type d | wc -l`

## 배포 후

- [ ] 워커 3개 kickstart → `ps -eo pid,lstart` 로 시작 시각이 방금인지 확인
- [ ] `ps -Ewwo command= -p <pid>` 에 **`BSVIBE_WORKER_WORKSPACE_ROOT` 가 없다**

## 동작

- [ ] server_sandbox 런을 하나 쏜다
- [ ] 워커 자식의 cwd 가 **`~/.bsvibe/sandbox-cwd`** 다
      `lsof -p <claude pid> -a -d cwd -Fn`
- [ ] 런이 끝난 뒤에도 그 디렉터리가 **그대로 있다**(`rmtree` 되지 않는다)
- [ ] **두 번째 런의 cwd 가 첫 번째와 같다** — 이게 이 변경의 핵심 단언이다
- [ ] `~/.bsvibe/sandbox-cwd` 안에 **태스크별 하위 디렉터리가 생기지 않는다**

## 회귀 (이번 변경이 깰 수 있는 것)

- [ ] **`client_attach` 런이 사용자 디렉터리에서 그대로 돌고, 그 디렉터리가 삭제되지 않는다**
      (#692 — 이게 깨지면 사용자 작업물이 사라진다. 가장 위험한 칸)
- [ ] `client_attach` 대상 디렉터리가 없을 때 **여전히 크게 실패**한다(임시 디렉터리로 폴백하지 않는다)
- [ ] **동시 런 2개 이상**이 같은 cwd 를 써도 둘 다 정상 완료한다
      (`max_parallel_tasks=3` — 유닛으로는 못 박았지만 실제 CLI 로는 이번이 처음이다)

## 효과 확인 (이 변경의 목적)

- [ ] 런을 N 개 돌린 뒤 `~/.claude/projects` 항목 수가 **베이스라인 + 1 이하**다
      (옛 동작이면 런마다 +1 이었다 — 2026-09-16 실측 994개 중 **972개**가 `bsvibe-task-*`)

## 무관함을 확인 (주장하지 않기 위해)

- [ ] **프레이밍 행이 고쳐졌는지는 이 체크리스트의 통과 조건이 아니다.**
      고쳐졌다면 기록하되, 안 고쳐져도 이 PR 은 유효하다 — 근거는 litter 제거와 단순화이지
      행 수정이 아니다. (행의 원인은 2026-09-16 현재 **미상**이고, 이 세션에서 세운 가설 넷은
      전부 틀렸다 — #965 참고)
