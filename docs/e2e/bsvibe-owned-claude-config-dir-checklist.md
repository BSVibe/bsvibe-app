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

## 배포 전 — 베이스라인

- [ ] 호스트 `~/.claude/projects` 항목 수를 기록한다 (#973 실측: 994개 중 972개가 `bsvibe-task-*`)
      `find ~/.claude/projects -maxdepth 1 -type d | wc -l`
- [ ] `ls ~/.bsvibe/` — `claude-config` 가 **아직 없어야** 한다
- [ ] 워커 OAuth 가 살아 있는지 확인 — **이게 선행조건이다**
      `python3 -c "import json,os,datetime;d=json.load(open(os.path.expanduser('~/.bsvibe/claude_oauth.json')));print(datetime.datetime.fromtimestamp(d['expires_at']/1000))"`

## 배포 후 — 리다이렉트가 먹었나

- [ ] 워커 3개 kickstart → 시작 시각 확인
- [ ] 런을 하나 쏜다 → **완주한다**(여기가 깨지면 인증이 깨진 것이다. 즉시 롤백)
- [ ] `~/.bsvibe/claude-config/` 가 **생겼고** 안에 `.claude.json` · `projects` ·
      `sessions` · `backups` 가 있다
- [ ] **양성 대조군**: 그 디렉터리가 실제로 **쓰이고 있다** —
      `find ~/.bsvibe/claude-config -newermt '-10 minutes' | head`
      비어 있으면 변수가 전달되지 않은 것이고, **그래도 런은 완주하므로 조용히 실패한다**
- [ ] **음성 대조군**: 호스트 `~/.claude/projects` 에 **새 `bsvibe-*` 항목이 안 생긴다**
      (배포 전 개수와 비교. 늘어나면 리다이렉트가 안 먹은 것)

## 동작 — 노출면이 실제로 닫혔나 ⭐

`--debug-file` 로 재라. 유닛 실측(배포 코드가 만든 env, 실제 CLI):

| 경로 | 이전 | 이후 |
|---|---|---|
| `~/.claude/` | 10 | **0** |
| `~/.claude/plugins` | 2 | **0** |
| BSVibe config dir | 0 | **39** |
| `/Library/Application Support/ClaudeCode` | 9 | 9 |

- [ ] prod 워커가 띄운 자식으로 같은 수치를 재현한다
- [ ] **#965 재현 시도**: 호스트 `~/.claude/plugins/known_marketplaces.json` 의
      `installLocation` 을 다시 `/home/vscode/...` 로 **일부러 되돌리고** 런을 쏜다 →
      **행이 나지 않아야 한다**. ⚠️ **먼저 파일을 복사해 두라.** 이게 이 PR 의
      존재 이유를 직접 증명하는 유일한 실험이다
- [ ] 되돌린 값을 원복한다

## 회귀

- [ ] **agentic 런**(MCP 경로)이 그대로 돈다 — 두 분기가 같은 env 를 쓴다
- [ ] `client_attach` verify 게이트가 그대로 돈다
- [ ] 워커 크리덴셜을 **일부러 태우고**(백업 후 `refresh_token` 을 쓰레기로) 런을 쏜다 →
      `claude_oauth_cli_fallback_used` 가 뜨고 **여전히 완주한다**. 원복
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
