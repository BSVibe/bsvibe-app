# E2E — CLI 의 config 디렉터리가 BSVibe 소유다 (#978)

**대상 PR**: `CLAUDE_CONFIG_DIR` 을 `~/.bsvibe/claude-config` 로
**전제**: 호스트 워커는 **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 후
**프로세스 시작 시각**으로 확인.

> 🚨 **이 변경의 안전성은 `ANTHROPIC_AUTH_TOKEN` 주입에 전적으로 의존한다.**
> 2026-09-16 에 같은 리다이렉트가 `Not logged in` 으로 거절됐던 이유가 그것이다 —
> 그날은 워커 OAuth 가 만료돼 CLI 가 **호스트 크리덴셜 파일**로 인증하고 있었고,
> 그 파일은 지금 옮기는 바로 그 디렉터리 안에 산다.
>
> | | 호스트 config dir | BSVibe config dir |
> |---|---|---|
> | 토큰 주입됨 | 인증됨 | **인증됨** |
> | 토큰 없음 | 인증됨(호스트) | **`Not logged in`** |
>
> 생명줄은 살아남는다 — 크리덴셜 두 경로 모두 **우리 Python 이 `Path.home()` 으로
> 직접 읽어** env 로 주입한다. 워커 크리덴셜을 일부러 태워 실측했다.

---

> ✅ **2026-09-17 배포에서 걸었다.** prod `d9b05e0`. §미실행 은 문서 끝에.

## 배포 전 — 베이스라인

- [x] 호스트 `~/.claude/projects` 항목 수를 기록한다 → **664** (#973 실측: 994개 중 972개가 `bsvibe-task-*`)
      `find ~/.claude/projects -maxdepth 1 -type d | wc -l`
- [x] `ls ~/.bsvibe/` — `claude-config` 가 **아직 없어야** 한다 → 없음 ✅
- [x] 워커 OAuth 가 살아 있는지 확인 — **이게 선행조건이다** → 만료 21:38, 살아 있음
      `python3 -c "import json,os,datetime;d=json.load(open(os.path.expanduser('~/.bsvibe/claude_oauth.json')));print(datetime.datetime.fromtimestamp(d['expires_at']/1000))"`

## 배포 후 — 리다이렉트가 먹었나

- [x] 워커 3개 kickstart → 시작 시각 확인 → 18:04:10
- [x] 런을 하나 쏜다 → **완주한다** → `review_ready`, claim 09:04:33 / 09:04:50
- [x] `~/.bsvibe/claude-config/` 가 **생겼고** 안에 `.claude.json` · `projects` ·
      `sessions` · `backups` 가 있다 → 18:04 생성, 넷 다 존재
- [x] **양성 대조군**: 그 디렉터리가 실제로 **쓰이고 있다**
      → `projects/-Users-blasin--bsvibe-sandbox-cwd/` 에 세션 JSONL **2개**
- [x] **음성 대조군**: 호스트 `~/.claude/projects` 에 **새 항목이 안 생긴다**
      → **664 → 664** (변화 없음)

## 동작 — 노출면이 실제로 닫혔나 ⭐

`--debug-file` 로 재라. 유닛 실측(배포 코드가 만든 env, 실제 CLI):

| 경로 | 이전 | 이후 |
|---|---|---|
| `~/.claude/` | 10 | **0** |
| `~/.claude/plugins` | 2 | **0** |
| BSVibe config dir | 0 | **39** |
| `/Library/Application Support/ClaudeCode` | 9 | 9 |

- [ ] prod 워커가 띄운 자식으로 같은 수치를 재현한다 → **미실행**(아래 재현 실험이
      더 강한 증거라 생략). 유닛 쪽은 배포 코드가 만든 env 로 실제 CLI 에 태워 실측했다
- [x] ⭐⭐ **#965 재현 — 통과.** `installLocation` 을
      `/home/vscode/.claude/plugins/marketplaces/claude-plugins-official` 로
      **일부러 주입**하고 런을 쐈다. 09-16 에 prod 전체를 300초 타임아웃으로 멈춘 것과
      **똑같은 상태**인데 → **40초 만에 `review_ready` 완주**. 태스크 claim 09:05:36 ·
      09:05:51, 지연 0
- [x] 되돌린 값을 원복한다 → 백업과 **바이트 단위 동일** 확인. 원복 후 런도 정상

      ⚠️ **원래 값을 여기 적어 둔다** — 백업을 scratchpad 에 두면 세션과 함께 사라져
      *없는 파일을 가리키는 포인터*가 된다(09-16 인수인계 §0 이 같은 실수를 지적했다):
      `"installLocation": "/Users/blasin/.claude/plugins/marketplaces/claude-plugins-official"`

## 회귀

- [x] **agentic 런**(MCP 경로)이 그대로 돈다 — 실제 CLI 로 별도 확인했다
- [ ] `client_attach` verify 게이트가 그대로 돈다
- [x] 워커 크리덴셜을 **일부러 태운** 상태의 폴백 → `claude_oauth_cli_fallback_used`
      → 빌린 토큰 → **리다이렉트된 CLI 로 정상 응답**. prod 파일은 안 건드리고
      임시 파일로 시뮬레이션했다
- [ ] 대화형 `claude` (형님이 직접 쓰는 것)는 **영향 없다** — 워커 서브프로세스 env 에만 건다

## 부정 대조군

- [ ] `BSVIBE_WORKER_CLAUDE_CONFIG_DIR` 을 **쓸 수 없는 경로**로 지정하고 워커를 띄운다 →
      `claude_config_dir_unavailable` 경고가 뜨고 **런은 계속 완주한다**(fail-open)
- [ ] 그 경고가 안 뜨면 fail-open 이 조용히 일어난 것이다 — 그게 이 가드가 막는 것

## §미실행 / 남는 것

- [ ] `/Library/Application Support/ClaudeCode` **9건은 안 닫힌다.** 이 호스트에
      **그 경로가 존재하지 않고**(프로브만 한다) 쓰려면 **root 가 필요하다** —
      로컬 도구가 아무렇게나 쓰는 `~/.claude` 와 위험 등급이 다르다.
      ⇒ 이 PR 의 주장은 **"config 디렉터리가 우리 것이다"** 이지
      *"CLI 가 호스트 경로를 하나도 안 읽는다"* 가 아니다. 주석도 그렇게 적었다
- [ ] **테넌트 간 공유는 그대로다.** config dir 는 워커당 하나라 `projects`/`sessions`
      가 테넌트 간 공유된다 — 다만 **우리 것이 되어 지울 수 있다**. auto-memory 는
      #981 의 `autoMemoryEnabled: false` 가 이미 막는다. #991(워커 identity 공유)과
      같이 봐야 할 축
