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

- [ ] **245MB(실은 776MB) 단일 파일 로테이션** → **이 PR 에 없다.**

  위의 세 변경이 볼륨의 대부분을 없애지만 **상한을 두지는 않는다.** launchd 가
  `StandardOutPath` 의 fd 를 **열어 쥐고 있어서**, `newsyslog` 가 파일을 옮겨도
  워커는 **옮겨진 inode 에 계속 쓴다** — 로테이션 후 **워커 재시작이 있어야만**
  fd 가 새 파일을 잡는다(배포 때마다 하는 kickstart 가 마침 그 역할을 한다).

  ⇒ 남은 선택지는 **① `/etc/newsyslog.d/` 설정 + 배포 시 kickstart 에 의존**(sudo 필요,
  형님 손) 또는 **② 워커가 스스로 회전 파일에 쓰기**(launchd 리다이렉션을 버리는
  구조 변경). **①이 훨씬 싸지만 sudo 가 필요하다.** #970 에 남겨 둔다.

- [ ] **커넥터 경고 문서화** — `⚠ claude.ai connectors are disabled because
      ANTHROPIC_API_KEY or another auth source is set` 는 **정상**이다. 워커가 의도적으로
      `ANTHROPIC_AUTH_TOKEN` 을 주입해서 나오는 것이고 행과 무관하다(#970 코멘트에서
      이미 확정). 런북에 넣을 자리를 아직 안 정했다
