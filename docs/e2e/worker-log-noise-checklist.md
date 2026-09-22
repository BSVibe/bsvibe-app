# E2E — 워커 로그가 읽을 수 있는 크기와 모양으로 돌아왔다 (#970)

**대상 PR**: 확정 실패 한 줄화 · 백오프 · 데몬 로깅 설정
**전제**: 호스트 워커는 **autodeploy 안 된다.** 배포 후 반드시
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 하고,
**프로세스 시작 시각**으로 새 코드인지 확인할 것.

> 🚨 **로그 형식이 바뀐다.** ANSI 색이 박힌 `2026-09-17 16:00 [info ] event key=val`
> 에서 **JSON 한 줄**로. 기존 grep·꼬리보기 습관과 **다른 스킬/문서의 예시가 전부
> 옛 형식**이다. 갱신 대상: `docs/e2e/task-claim-and-redelivery-checklist.md` 의
> `task_claimed` grep, 운영 중 쓰는 `grep '\[warning' ` 류.

> ⚠️ **기본 레벨이 `info` 로 올라간다.** `claude_auth_keepalive_ok` 는 debug 라
> **더 이상 안 보인다**(그게 10,629줄이었다). 되살리려면
> `BSVIBE_WORKER_LOG_LEVEL=debug`.

> 📏 **2026-09-22 재측정 — 경고 1 은 원인째 사라졌다.** 로테이션 이후 4일치 라이브 로그
> (`mac-mini-e2e` 311줄 · `admin` 56줄, 09-18 12:22 ~ 09-22 02:05):
> `claude_oauth_refresh_invalid_grant` **0** · `claude_oauth_refresh_failed` **0** ·
> 트레이스백 **0** · 비-JSON 줄 **0**.
> **양성 대조군**: 같은 창에 `claude_oauth_refreshed` **12건**(성공) — 갱신기는 켜져 있고
> 이제 성공한다. 0 이 "생산자가 꺼져서"가 아니다.
> 남은 소음 1위는 `server_unreachable`/`http_error 502` 인데 4일 38줄이고 두 워커가
> **같은 시각**에 찍는다 ⇒ 배포 블립이지 결함 아니다.
> ⚠️ 탐지기가 지금 코드의 철자와 같은지 먼저 확인했다 —
> `claude_auth.py` 가 여전히 `claude_oauth_refresh_invalid_grant` 를 쓴다.

---

## 배포 전 — 베이스라인 (안 재면 개선을 주장할 수 없다)

> ✅ **2026-09-17 배포에서 걸었다.** prod `9258f51`. 미실행 항목은 §미실행 에.

- [x] 세 로그 파일의 **크기와 줄 수**를 기록한다
      `ls -l ~/Library/Logs/bsvibe-worker*.log; wc -l ~/Library/Logs/bsvibe-worker*.log`
      → 2026-09-17 실측: **253M + 259M + 264M**, 264M 파일이 **1,817,107줄**
- [x] 그중 **트레이스백/박스 문자 비율**을 기록한다 → 실측 **96.4%**
- [x] `claude_oauth_refresh_failed` · `claude_oauth_refresh_invalid_grant` 건수
      → 실측 **6,164 / 6,163** (= 실패의 99.98%가 invalid_grant)

## 배포 후 — 형식

- [x] 워커 3개 kickstart → 시작 시각 확인 → 17:48:58
- [x] 새로 찍힌 줄이 **JSON 한 줄**이고 **ANSI 이스케이프가 없다**
      → 재기동 이후 **11줄 전부 JSON, ANSI 0, 박스/트레이스백 0**
      ⚠️ **`tail -N` 으로 세지 마라.** 옛 형식 줄까지 딸려 들어와 오염된다 —
      이번에 실제로 한 번 오판했다. **`worker_starting` 마커 이후만** 세라
- [x] `jq` 로 파싱된다 — 이게 이번 변경의 진짜 값이다
      → `task_claimed` 2 · `task_received` 2 · `executor_turn_started` 2 ·
      `executor_turn_first_event` 2 · `task_completed` 2 · `worker_starting` 1
- [x] 모든 줄에 `"service": "bsvibe-worker"` 가 있다
- [x] `claude_auth_keepalive_ok` 가 **안 보인다**(레벨 info). **양성 대조군 통과**:
      `info`→숨김, `debug`→보임. `BSVIBE_WORKER_LOG_LEVEL=debug` 로 설정값이
      `'debug'` 로 바뀌는 것까지 확인 — 설정이 실제로 읽힌다
      ⚠️ ~~형식 전환 지점이 로그에 그대로 보인다: `worker_config_loaded`(옛 콘솔
      형식) **직후부터** JSON. `configure_logging` 이 `settings` 해석 뒤라 그 한 줄만
      옛 형식으로 남는다 — 정상이다~~ → **2026-09-17 후속으로 닫았다.**
      `_apply_persisted_config` 를 `configure_logging` **뒤로** 옮겼다
      (`log_level` 을 안 건드리므로 해석값이 안 바뀐다).
      "한 줄이니 정상"이 놓친 것은 **실패 경로**다 — `_apply_persisted_config` 가
      raise 하면 그 트레이스백이 **설정 안 된 rich 기본값**으로 렌더된다.
      데몬이 *왜 못 뜨는지* 말하는 바로 그 순간에 284줄 패널이 나오는 셈이고,
      그게 #970 이 없애려던 모양 그 자체다.
      🚨 그리고 이걸 지키던 테스트는 **초록이었다** —
      `test_the_worker_daemon_configures_its_logging` 의 독스트링은
      *"before anything can log"* 라고 적혀 있었지만 단언은 *호출됐는가* 둘뿐이라
      **순서를 잴 수 없었다.** 순서는 이제
      `test_the_daemon_configures_logging_before_its_first_log_line` 가 잰다

## 동작 — 소음이 실제로 줄었다

- [x] 런을 하나 태우고 **하루 뒤** 로그 증가분을 잰다. 배포 전 하루치와 비교
      → **2026-09-17 에 쟀다. 창은 82분이고 그 안에 실제 agentic 런
      하나(13분 53초, prompt 8.9M 토큰)가 들어 있다. 배포 전은 하루 전체를 썼다.**

      | | 배포 전 (09-16, 워커 **1개**) | 배포 후 (82분, 워커 **3개** 합) |
      |---|---|---|
      | 총 줄 | 85,908 | **39** |
      | 바이트 | 13,179,982 (**12.6 MB/일**) | 7,020 (**≈121 KB/일** 환산) |
      | ANSI 포함 줄 | 84,360 | **0** |
      | JSON 줄 | 0 | **39 (100%)** |

      ⭐ 눈여겨볼 것: **14분짜리 agentic 런 하나가 로그를 3줄 늘렸다**
      (`task_claimed` · `task_received` · `task_completed`). 런 길이가 로그
      볼륨을 끌지 않는다 — 볼륨을 끌던 것은 길이가 아니라 **반복되는 예외**였다.

      ⚠️ **이 배수를 #970 혼자의 공으로 읽지 마라.** 09-16 하루치를 분해하면
      이벤트 헤더는 **1,252줄**뿐이고 나머지 **84,656줄(98.5%)이 트레이스백
      연장선**이다. 그 트레이스백은 `claude_oauth_refresh_failed` **296건**
      × 약 284줄로 거의 전부 설명된다. 즉 —
      * **#987~#989(OAuth 재로그인)** 가 그 296건을 **없앴다**
      * **#993(#970, `configure_logging`)** 은 예외 **한 건의 단가**를
        284줄 → 1줄로 바꿨다

      두 개는 곱해진다. 내일 같은 인증 장애가 재발해도 하루 84,064줄이 아니라
      **296줄**이 된다 — #970 이 실제로 산 것은 볼륨이 아니라 **그 꼬리 위험**이다.

      ⚠️ 창이 70분이라 일 환산은 약하다. 실측으로 단단한 것은 **구성**
      (ANSI 0 · JSON 100% · 트레이스백 0)이고, 그건 창 길이와 무관하다.
- [x] 예외가 나는 줄이 **한 줄**이다 — `"exception"` 필드 안에 문자열로 들어간다
      (유닛 실측: 14줄·1909B → **1줄·277B**, ANSI 제거. prod 스택은 더 깊어 감소폭이 더 크다)

> ⚠️ **미실행**: 확정 실패 시나리오(아래 ⭐)는 **워커 크리덴셜을 일부러 태워야** 보이는데,
> 그건 prod 인증을 만지는 일이라 이번 배포에서는 안 걸었다. 단위 쪽은 전선 절단 7건으로
> 전부 덮여 있고, `#978` 검증 중 **폴백 경로 자체는 실측으로 확인**했다
> (워커 크리덴셜을 태운 상태에서 `claude_oauth_cli_fallback_used` → 정상 응답).

## 동작 — 확정 실패 ⭐

여기가 이 PR 의 핵심이다. **인위적으로 만들어야 보인다.**

- [ ] `~/.bsvibe/claude_oauth.json` 의 `refresh_token` 을 **쓰레기 값으로 바꾸고**
      `expires_at` 을 과거로 만든다 (⚠️ **먼저 파일을 복사해 두라**)
- [ ] 워커를 kickstart → `claude_oauth_refresh_invalid_grant` 가 **한 줄**,
      그 뒤에 `claude_oauth_refresh_failed` 가 **따라오지 않는다**
- [ ] `claude_auth_keepalive_definitive_failure` 의 `retry_in_s` 가
      **300 → 600 → 1200 → …** 로 커지고 **3600 에서 멈춘다**
- [ ] 그동안 런은 **계속 완주한다** — CLI 폴백이 살아 있다(이 PR 은 폴백을 안 건드린다)
- [ ] 백업 파일을 되돌리고 워커 kickstart → `retry_in_s` 가 **300 으로 돌아온다**
      (= 복구 시 리셋. 이게 없으면 형님이 재로그인해도 최대 1시간 모른다)

## 회귀

- [ ] `bsvibe-worker register` · `claude-login` 등 **대화형 CLI** 의 출력이
      읽을 만한가. ⚠️ `_amain` 에만 설정을 걸었으므로 대화형 경로는 안 바뀌어야 한다
- [ ] 예상 못 한 에러(네트워크 단절 등)는 **여전히 트레이스백을 남긴다** —
      전선 끊기: 서버를 내리고 `claude_oauth_refresh_failed` 에 `exception` 필드가 있는지
- [ ] 런 완주 · claim 영수증(#965)이 그대로 동작한다

## §미실행 — 로그 로테이션

- [x] ~~**245MB(실은 776MB) 단일 파일 로테이션**~~ → **닫혔다** (#970 코멘트, `_infra` `26bf6cd`).
  아래 "sudo 필요"라는 전제가 **틀렸다** — launchd 리다이렉트는 `O_APPEND` 라 `copytruncate`
  는 재시작이 필요 없다. 시간당 user-level(gui/501) 에이전트로 **805M → 64M**, 워커 PID 불변.
  ⚠️ 아래 문단은 **그 틀린 전제를 남겨 둔 원문**이다 — 왜 두 선택지뿐이라고 믿었는지의 기록.

- [ ] ~~**245MB(실은 776MB) 단일 파일 로테이션** → **이 PR 에 없다.**~~

  위의 세 변경이 볼륨의 대부분을 없애지만 **상한을 두지는 않는다.** launchd 가
  `StandardOutPath` 의 fd 를 **열어 쥐고 있어서**, `newsyslog` 가 파일을 옮겨도
  워커는 **옮겨진 inode 에 계속 쓴다** — 로테이션 후 **워커 재시작이 있어야만**
  fd 가 새 파일을 잡는다(배포 때마다 하는 kickstart 가 마침 그 역할을 한다).

  ⇒ 남은 선택지는 **① `/etc/newsyslog.d/` 설정 + 배포 시 kickstart 에 의존**(sudo 필요,
  형님 손) 또는 **② 워커가 스스로 회전 파일에 쓰기**(launchd 리다이렉션을 버리는
  구조 변경). **①이 훨씬 싸지만 sudo 가 필요하다.** #970 에 남겨 둔다.

- [x] ~~**커넥터 경고 문서화**~~ → **문서화가 아니라 껐다.** 아래 §커넥터 경고 참조.

  이 칸이 *"못 끄면 정상 동작으로 문서화"* 라고 적혀 있던 것은 **끌 수 있는지를 안 재서**다.
  재니 노브가 둘 있었고, 하나는 우리가 **이미 들고 있는 `--settings` blob** 안이었다.

## §커넥터 경고 — 껐다 (2026-09-22)

**대상 PR**: `_FORCED_CLI_SETTINGS` 에 `disableClaudeAiConnectors: true`

매 CLI 호출이 stderr 로 찍던 줄:

```
⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or another auth source
  is set and takes precedence over your claude.ai login · Unset it to load your
  organization's connectors
```

방아쇠는 `ANTHROPIC_AUTH_TOKEN` 이 **설정되어 있다는 사실**이고, 그건 워커가 **일부러**
넣는 것이다(launchd 로 뜬 claude 가 Keychain 을 못 읽는다). 즉 이 경고는 **설계대로
동작 중임을 알리는 경고**였고, 두 번의 조사에서 *"인증 경로가 엉뚱한 가지로 갔다"* 로
읽혀 시간을 썼다.

### 배포 전 — 실측 (CLI 2.1.268, 워커 자신의 chat 플래그 + 더미 토큰)

> 토큰 **값**은 무관하다고 #970 이 이미 실측했다. 진짜 refresh 토큰은 단발성이라
> 디버깅으로 태우면 안 되므로 더미를 썼다.

- [x] 번들에서 그 문장의 **가드를 직접 읽었다** — 경고는 `api_key_precedence` 분기에서
      세팅되고, 그 **앞에 early return** 이 있다(`disableClaudeAiConnectors` 설정 또는
      `ENABLE_CLAUDEAI_MCP_SERVERS` env). 상상한 철자를 grep 한 게 아니다
- [x] 노브가 **양방향으로 뒤집힌다** — 한쪽 판정만 내는 검사가 아니다

      | 잰 것 | stderr |
      |---|---|
      | (노브 없음) | ⚠️ 나온다 — **양성 대조군** |
      | `--settings disableClaudeAiConnectors=true` | **없다** |
      | `--settings disableClaudeAiConnectors=false` | ⚠️ 나온다 — **대조군** |
      | `ENABLE_CLAUDEAI_MCP_SERVERS=0` / `=false` | **없다** |
      | `ENABLE_CLAUDEAI_MCP_SERVERS=1` | ⚠️ 나온다 — **대조군** |

      ⇒ 경고를 없앤 것은 **그 노브**다. 내 하네스가 아니다
- [x] 🚨 **툴 도착이 안 깨진다** — 워커의 가장 치명적 실패 모드가 *"BSVibe 툴이 안 왔다"*이다.
      스크래치 stdio MCP 서버 + 실제 스트리밍 경로(`--output-format stream-json`)로
      노브 on/off 를 각각 돌려 `system/init` 을 읽었다:
      **양쪽 다** `mcp_servers: [{"status": "connected"}]` + 그 서버의 툴 존재.
      코드도 같은 말을 한다 — 워커 상황에서 커넥터 조회는 **이미** "없음"을 돌려주고 있었고,
      노브는 **어느 early return 으로 거기 가느냐**만 바꾼다. claude.ai 커넥터는
      `--mcp-config` 서버와 **다른 네임스페이스**다
- [x] 첫 이벤트 지연의 원인이 아니다 — #970 이 *"커넥터 로딩이 첫 턴에서 시간을 쓰는지
      확인된 바 없다"* 고 적었던 칸. prod 로그의 `executor_turn_first_event` **15/15** 가
      `elapsed_s` **0.228~1.501**(중앙값 0.87). ⇒ 이건 **지연이 아니라 소음**이었다
- [x] 전선 절단 3건, 전부 컴파일됐고(매번 **35개 실행**) 정확히 의도한 칸만 뒤집혔다:
      새 키 제거 → 새 테스트 3건만 · `autoMemoryEnabled` 제거 → auto-memory 4건 ·
      agentic 호출 지점에서 blob 제거 → **그 분기의 5건**(호출 지점별로 따로 잡힌다)

### 배포 후 — 걸 것

- [ ] 워커 kickstart → **프로세스 시작 시각**으로 새 코드 확인
- [ ] 배포된 코드가 만드는 **실제 argv** 를 찍어(`_build_cmd_args`) 그 argv 의
      `--settings` 에 `disableClaudeAiConnectors` 가 들어 있는지 본다
- [ ] 그 **argv 그대로** CLI 를 한 번 돌려 stderr 에 경고가 **없음**을 확인하고,
      같은 argv 에서 **그 키만 뺀** 쌍을 돌려 경고가 **돌아오는지** 확인한다
      (⚠️ 없음만 재면 내가 뭘 껐는지 증명 못 한다)
- [ ] 🚨 **실제 agentic 런을 하나 완주**시킨다 — `system/init` 이 `connected` 이고
      BSVibe 툴이 도착하는지. 로컬 실측이 덮지만 **prod 의 원격 MCP 서버**는 다른 조건이다
- [ ] chat 턴도 하나 — 두 분기가 각각 배선돼 있다
