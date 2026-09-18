# BSVibe 세션 인수인계 — 2026-09-17 (2판)

**워커**: 호스트 3개 — **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 후 **프로세스 시작 시각**으로 확인
**열린 작업은 이 문서가 아니라 GitHub 이슈에 있다.** 열린 PR·워크트리는 `gh pr list` / `git worktree list`.

> 🧭 **이 헤더에 전이적인 것을 적지 마라.** 자기 자신을 가리키는 필드는 이 문서를
> 머지하는 순간 거짓이 된다 — 값을 갱신하는 걸로는 못 고치고 **필드를 없애야 고쳐진다.**

> 📦 **09-16 판과 09-17 1판은 아직 Notion 아카이브로 안 옮겼다.** git 에는 남아 있다.

---

## §0 — 이 세션이 한 일 한 줄 요약

앞 세션이 *"다음 **긴 런 하나**로 세 개를 한꺼번에 봐라"* 로 넘긴 칸을 채웠고,
**채우는 과정에서 네 가지가 더 나왔다** — 그중 둘은 **앞 세션이 "정상" 또는
"불가능"으로 적어 둔 판정을 뒤집는다.**

### 이 세션의 산출물

| | |
|---|---|
| PR **#998** | `_amain` 의 로깅 설정 순서 (#970) — CI ✅ · **머지·배포·prod 확인 완료** |
| PR **#999** | #965·#966 긴 런 검증 결과 — CI ✅ · **머지됨** |
| PR **#1001** | TMPDIR 누수 수정 (#997) — CI ✅ · **머지됨** |
| #967 코멘트 | docs 한 파일 PR 에서 터진 flake — 인과 배제가 공짜인 데이터 |
| 이슈 **#997** | TMPDIR 누수 (개설) |
| 이슈 **#1000** | codex·opencode MCP 표면 측정 (개설) |
| **#928** 코멘트 | 한 턴이 런 상한의 4.5배 — 실측 |

✅ **#998 워커 kickstart 완료** — prod 확인까지 §Ⅴ.5.

## §Ⅰ — ⭐ 긴 런 검증: 세 칸 다 찼다 (PR #999)

태스크 `6af37139` · 런 `143d048c` · prod `8ede21b` · `agentic=t`

| | |
|---|---|
| 턴 길이 | **832.6초 = 13분 53초** |
| 토큰 | prompt **8,922,660** / completion 65,588 |
| `executor_task_redelivered` | **0** (40초 간격 21회 샘플) |
| 워커 도착 | **정확히 1회** (`task_claimed`·`task_received`·`task_completed` 각 1줄) |
| claim 지연 | **2.43초** |
| #966 3번 (안 잘림) | ✅ `done`, `act` 예산 3600s 의 23% |

**이게 왜 설계의 근거인가**: 재전달 유예가 30초다. lease 였다면 832.6 ÷ 30 ≈ **27회**
중복 실행 기회가 있었다. 실제 0회 — 축이 타이머가 아니라 `claimed_at IS NULL` 이라
**길이가 아무 역할을 안 한다.** 8.9M 토큰짜리 턴이 두 번 돌면 비용이 두 배다.

⚠️ **양성 대조군을 같이 걸었다**: 같은 스트림·grep 으로 `executor_task_dispatched`
가 **0 → 2 로 움직였다.** 안 움직이는 카운터의 0 은 "일어나지 않았다"가 아니라
"여기엔 없다"일 수 있다 — 09-17 1판이 실제로 밟은 오판이다.

## §Ⅱ — 🚨 그 런이 덤으로 드러낸 것: 한 턴이 런 상한의 4.5배 (#928 에 코멘트)

`agent_max_run_tokens` = **2,000,000**. 그 턴 하나가 **8,988,248** 을 썼다.

결함이 아니다 — `token_budget.py` 가 *"the turn-count budget cannot bound a single
huge turn"* 이라고 이미 적었고, 집행은 턴 **경계**에서만 가능하다. 새로운 건 **배수**다:
`act` 형 작업에서는 **첫 턴 하나가 런 상한을 몇 배로 넘긴다.** 계상은 정상이었고
(8,988,248 이 행에 올라갔다) **막을 지점이 없었다.**

⇒ #928 설계에 축이 하나 더 필요하다: **턴 내부에서 상한을 볼 수 있는가.** 지금은 못 본다.
prompt 가 8.9M 중 대부분이므로 **요청 시점 입력 상한**이 가장 큰 레버로 보인다(미확인).

⚠️ 그 런은 지금 `run_token_cap_reached` Decision `db492740` 에 **parked** 다.
**동시 런 슬롯 3개 중 1개를 잡고 있다** — 형님이 retry/discard 를 정해야 풀린다.
생산물은 있다(`crypto.py`·`rotate_credentials.py` + 테스트 2개, 샌드박스 안).

## §Ⅲ — 🚨 앞 세션의 판정 두 개가 뒤집혔다

### ① "한 줄이니 정상이다" → 아니었다 (PR #998)

09-17 1판이 `worker_config_loaded` 가 옛 콘솔 형식으로 남는 걸 보고 **"정상이다"**
로 적었다. 볼륨만 보면 맞다(재기동당 3줄). 놓친 건 **실패 경로**다 —
`_apply_persisted_config` 가 raise 하면 그 트레이스백이 **설정 안 된 rich 기본값**으로
렌더된다. 데몬이 *왜 못 뜨는지* 말하는 순간에 284줄 패널이 나온다.

**그리고 그걸 지키던 테스트는 초록이었다.** 독스트링은
*"must configure logging **before anything can log**"* 인데 단언은
*호출됐나* 둘뿐 — **"호출됐다"는 관찰 두 개 사이에는 순서가 없다.**

🔁 **내 첫 수정도 불완전했고, 내 새 테스트도 그걸 못 잡았다** — 두 단계로:
1. 테스트가 `_ensure_process_group`(얘도 로깅한다)을 **no-op 으로 패치**했다
2. 패치를 걷어내도 못 잡았다 — pytest 밑에선 `os.setpgrp()` 가 **성공**해서
   로그를 내는 except 분기가 **안 돈다.** 전선을 끊어도 초록이었다
⇒ `os.setpgrp` 가 OSError 를 내도록 **강제**해서 분기를 열었다.

### ② codex "NO flag disables it" → 같은 버전에서 뒤집혔다 (#1000)

`codex.py` 가 *"its shell/exec tool is intrinsic and **NO flag disables it**
(verified against codex 0.130.0's own binary)"* 라고 단정한다.
**`codex features list` 에 `shell_tool stable true` 가 있고 `-c features.shell_tool=false`
로 꺼진다.** A/B(양성 대조군 포함): 기본은 `exec` 호출 + MARKER 5회,
끈 쪽은 `exec` **0회** + *"no shell execution tool is available"*.

⚠️ **버전조차 안 바뀌었다.** 만료된 건 조건이 아니라 **훑어본 노브의 열거**다 —
그리고 그 열거엔 `--config` 가 **이미 있었다**. 표면은 봤는데 그 아래
키 공간(`features.*`)을 안 열어 봤다.

## §Ⅳ — 형님 결정 반영: codex·opencode 는 MCP 표면을 받는다 (#1000)

**결정(09-17)**: agentic 제외가 아니라 **MCP 표면 부여 + 네이티브 박탈**.

| | ① HTTP MCP + 인증 헤더 | ② 네이티브 박탈 |
|---|---|---|
| **opencode 1.17.3** | ✅ `mcp add --url --header KEY=VALUE` | ✅ `tools` 맵 `{"*": false}` (라이브 검증됨) |
| **codex 0.130.0** | ✅ `mcp add --url --bearer-token-env-var` (헤더 아님, **env var**) | 🔶 `shell_tool` 은 꺼진다. **나머지는 안 쟀다** |

**codex 를 "통과"로 적지 않은 이유**: `shell_tool` **하나만** 껐다. `unified_exec`
(stable true) · `browser_use` · `computer_use` · `apply_patch_*` · `js_repl` 은 **안 쟀고**,
모델이 *"툴이 없다"* 고 **말한 것**은 툴 목록의 덤프가 아니다.

🚨 **덤으로 나온 노출면**: 빈 디렉터리에서 `codex exec` 를 돌렸을 뿐인데
**형님 개인 MCP 서버 3개(cloudflare·notion·vercel)와 memories DB 를 그대로 붙였다.**
#978 에서 claude CLI 에 대해 닫은 것과 **같은 노출면**이다. `CODEX_HOME` 이 있고
config 를 안 읽는 플래그도 있지만 **인증은 여전히 `CODEX_HOME` 을 쓴다** —
#978 의 2×2 함정 그대로다.

⚠️ **잠복 구멍은 따로다**: `supports_remote_tools` 게이트는 **chat 경로
(`adapter.py:491`)에만** 있다. agentic 디스패치의 `_work_tool_surface` 는
`executor_type` 을 **안 본다.** 위 다섯 칸과 **독립적으로** 닫아야 한다.

## §Ⅴ — TMPDIR 누수: 이슈 개설 + 수정 (#997, PR #1001)

09-17 1판이 "이슈 미개설"로 넘긴 항목.

| | |
|---|---|
| 디렉터리 | **18,739개** → 측정 중 **18,810** 으로 증가 |
| 용량 | **1,888.9 MB** |
| 가장 오래된 것 | **2026-08-15** (33일 생존) |
| 이 세션의 로컬 실행들 동안 증가 | **+71개** (18,739 → 18,810) |

⇒ 독스트링의 *"The temp dir is **left for the OS to reap**"* 가 **실측으로 틀렸다.**
macOS 는 안 걷어간다.

🚨 **셀수록 0 이 나온다**: 18,739개에서 `ls -d "$TMPDIR"bsvibe-shared-test-* | wc -l`
이 **0** 을 준다 — argv 한계로 죽은 에러가 stderr 로 새고 `wc` 는 빈 stdout 을 센다.
**누수가 커질수록 순진한 카운트가 0 을 보고한다.** 이번에 실제로 밟았다.
그리고 `find /var/folders -maxdepth 3` 도 0 이다 — TMPDIR 안은 **깊이 4**.
⇒ 세는 법: `find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'bsvibe-shared-test-*' -type d | wc -l`

수정은 `engine.dispose()` **뒤에** `shutil.rmtree`. 실증: 수정 후 **전체 스위트
(6367 passed)에서 누수 델타 0**. ⚠️ +71 은 깨끗한 A/B 가 아니다(수정 전 구간에
전체 1회 + 부분 몇 번이 섞였다) — 단단한 건 **한 명령으로 잰 델타 0** 쪽이다.

⚠️ **기존 18,810개는 안 지웠다** — 형님 확인 후 지울 것. 위 `find` 에 `-delete`.

## §Ⅴ.5 — 09-18 마감 처리 (형님 지시로 실행)

### 배포 + 워커 재기동, 그리고 #998 이 prod 에서 확인됐다

`bcb432a` autodeploy 완료. 호스트 워커 3개 kickstart(새 PID, 11:57:37~38).

**재기동 이후 12줄 = JSON 12 · ANSI 0 · 콘솔 형식 0.**
`worker_config_loaded` 가 이제 JSON 으로 나온다:

```json
{"sources": {"name": "env", "server_url": "env", ...}, "event": "worker_config_loaded", ...}
```

⇒ #998 전에는 **재기동마다 워커당 정확히 1줄**이 옛 콘솔 형식이었다. 이제 0 이다.
경계는 시간이 아니라 **재기동 직전 줄 수를 찍어** 그 뒤만 셌다.

### TMPDIR 정리 (#997)

**18,810개 삭제.** TMPDIR **1.9G → 100M**.
지우기 전에 확인한 것: 그 아래 존재하는 파일명이 `test.db*` **하나뿐**이었다.
⚠️ 양성 대조군 — 삭제 후 `find` 가 같은 위치에서 다른 디렉터리 **590개**를 여전히 찾는다.
그게 0 이 "정말 없다"의 근거다(순진한 `ls | wc -l` 은 커지면 거짓 0 을 준다, §Ⅷ).

### #957 런 Decision: **discard** 로 처리했다

`run_token_cap_reached` 는 **one-click action 이 없다** — `checkpoints_resolve` 가
거절한다. `bsvibe_runs_discard` 가 맞는 프리미티브다(펜딩 Decision 도 같이 정리하고
deliverable 을 tombstone 한다).

판단 근거 셋:
1. 이 런의 목적(긴 런 검증)은 **이미 달성**됐다 — PR #999
2. **산출물이 복구 불가**다. deliverable 에 `artifact_uri`·`diff` 가 없고,
   샌드박스는 턴마다 새 클론이라 커밋 안 된 파일은 사라진다. `var/runs` 는 **비어 있었다(0개)**.
   ⇒ **retry 는 이어가기가 아니라 처음부터**다
3. 다시 돌리면 또 900만 토큰급이 나가는데 **턴 내부에 상한을 볼 지점이 없다**(§Ⅱ)

⇒ **#957 은 열린 채로 둔다.** 암호 키 취급이라 검토를 붙인 구현으로 따로 해야 한다.
슬롯은 풀렸다(in-flight 런 0).

### #967 flake 에 증거 한 건 달았다

PR #1002(**`docs/HANDOFF.md` 한 파일**)의 CI 가
`test_exec_runs_command_on_worker_and_maps_zero_exit` 에서 `assert None == 0` 로 터졌다.
**diff 에 코드가 없으므로 인과가 원천적으로 없다** — 배제가 공짜인 데이터라 이슈에 적었다.
같은 시각 다른 CI 잡 **3건**이 겹쳐 돌았고 실패는 스위트 **83%** 지점이었다.

## §Ⅵ — 형님 손에 있는 것

1. **#991 워커 identity 분리** — launchd plist (그대로)
2. **#970 잔여 = 로그 로테이션 하나** — sudo (그대로)
3. 이월: **#935 시크릿 로테이션 2건** · **#937 리포 밖** · Notion 아카이브 2판

~~#957 런 Decision~~ · ~~TMPDIR 18,810개 삭제~~ → **09-18 에 처리했다(§Ⅴ.5)**

## §Ⅶ — 다음 세션 시작점

1. **#1000 codex·opencode** — 결정은 났고 **측정이 반쯤 됐다.** 다음 칸:
   codex 의 나머지 실행 플래그를 끈 뒤 **남은 툴 목록을 직접 덤프** ·
   `CODEX_HOME` 2×2 · opencode 의 **allowlist 방향**(`{"*": false}` + 우리 툴만 켜기)
2. **`supports_remote_tools` 가 agentic 경로에 없다** — 위와 독립. 작고 명확하다
3. **#928** — §Ⅱ 의 숫자를 **해상도** 측정으로 쓰고 **상한 값 튜닝에는 쓰지 마라**(#953 이전)
4. **#964 settle 스코프** · **#957**(런이 만든 코드가 샌드박스에 있다)

### 검증 안 된 것 (정직하게)

* **#970 하루치는 82분 창으로만 쟀다.** 단단한 건 **구성**(ANSI 0 · JSON 100% ·
  트레이스백 0)이고 그건 창 길이와 무관하다. 일 환산(≈121 KB/일)은 약하다
* **그 배수를 #970 혼자의 공으로 읽지 마라** — 09-16 하루치의 98.5%가
  `claude_oauth_refresh_failed` 296건의 트레이스백이었고, **그건 #987~#989 가 없앴다.**
  #970 이 산 것은 볼륨이 아니라 **꼬리 위험**(같은 장애 재발 시 84,064줄 → 296줄)
* **#965 부정 대조군 2건 · `client_attach` verify 게이트 · claim 500 fail-open ·
  `cancel` 경로** — 그대로 미실행. 이유는 `docs/e2e/task-claim-and-redelivery-checklist.md` §미실행
* **codex 의 나머지 실행 플래그** — §Ⅳ 표의 🔶

## §Ⅷ — 이 세션에서 값을 한 규율

* **⏳⭐⭐ 기각은 조건이 안 바뀌어도 만료된다.** codex 의 "NO flag disables it" 이
  **같은 버전 0.130.0 에서** 뒤집혔다. 만료된 건 조건이 아니라 **훑어본 노브의 열거**이고,
  그 열거엔 `--config` 가 **이미 있었다** — 표면만 보고 키 공간을 안 열었다.
  ⇒ "노브가 없다"를 적을 땐 **무엇을 훑었는지가 아니라 어떻게 훑었는지**를 적어라.
  벤더 CLI 는 기능을 플래그가 아니라 **feature flag / dotted config 키**에 숨긴다.
* **🩹⭐ "한 줄이니 정상"은 볼륨만 보고 실패 경로를 안 본 판정이다.** 평소 한 줄인
  코드가 **raise 할 때** 무엇을 뱉는지가 진짜 노출면이다.
* **🔪⭐⭐ 패치로 지운 것은 지켜지지 않고, 안 도는 분기도 지켜지지 않는다.**
  순서 테스트가 `_ensure_process_group` 을 no-op 으로 패치해 못 잡았고,
  패치를 걷어낸 뒤에도 **`os.setpgrp()` 가 성공해서 로그 분기가 안 돌아** 못 잡았다.
  전선을 두 번 끊고서야 빨개졌다. **초록은 가드의 위치를 증명하지 않는다.**
* **🔢⭐⭐ 누수는 커질수록 스스로를 숨긴다.** 18,739개에서 `ls | wc -l` 이 **0** 을 준다 —
  argv 한계 에러가 stderr 로 새고 `wc` 는 빈 stdout 을 센다. **"0" 을 봤을 때
  그 0 이 stdout 인지 에러인지 먼저 갈라라.** `maxdepth` 도 같은 종류로 0 을 만든다.
* **📍 긴 런 검증은 태스크 id 를 미리 못 박지 마라.** Direct 한 건이 태스크를 **둘**
  만든다 — 앞의 것은 분류용(27초, `agentic=f`, `run_id` 없음)이고 진짜 `act` 턴은
  **그 다음**이다. 첫 감시기를 앞엣것에 걸어 40초 만에 "끝났다"로 읽었다.
  ⇒ **`agentic=t` 이고 `run_id` 가 붙은 행**을 찾아서 걸어라.
* **🧭 배포 디렉터리에 워크트리를 만들지 마라.** `git -C main worktree add wt/x` 가
  `main/wt/x` 를 만들어 `?? wt/` 로 **autodeploy 를 영구 차단**할 뻔했다.
  `.bare` 에서 **절대경로**로 만들어라: `git -C .bare worktree add /…/wt/x`.
