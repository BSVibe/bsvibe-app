# BSVibe 세션 인수인계 — 2026-09-16

**prod**: `#974` 배포분 · **열린 PR**: 이 문서 · **워크트리**: `wt/handoff-0916`
**워커**: 호스트 3개 — **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 후 **프로세스 시작 시각**으로 확인
**열린 작업은 이 문서가 아니라 GitHub 이슈에 있다.**

> 📦 직전 판들(`a121086` · `8a5a152` · `4091196` · `5ebfff1`)은 **Notion 아카이브로 아직 못 옮겼다**
> — 이 세션에도 Notion 도구가 없었다. 원문은 `git show <sha>:docs/HANDOFF.md`.

---

## §0 — 먼저 읽어라: **프레이밍 행은 아직 안 고쳐졌고, 이 세션의 가설 넷은 전부 틀렸다**

**prod 의 모든 런이 여전히 프레이밍에서 죽는다.** 전량은
**[#965 코멘트 2건](https://github.com/BSVibe/bsvibe-app/issues/965)** — 첫 코멘트의 *"원인 확정(TCC)"* 은
**두 번째 코멘트가 반증한다. 첫 코멘트를 근거로 삼지 마라.**

### 살아 있는 사실은 하나뿐이다

**`claude` 가 `openat$NOCANCEL` 에서 영원히 블록한다** (`sample` 2243/2243 = 100%).
서브프로세스는 **정상 기동**하고(#968 마커가 1초 만에 찍힌다), 출력은 **단 한 줄도 없고**,
백엔드가 300초에 포기한다. **어느 경로를 여는지는 모른다.**

### 다음 사람이 할 단 하나의 측정 — 형님 손이 필요하다

```
sudo fs_usage -w -f filesys        # 걸어두고 프레이밍 런을 하나 쏜다
```

`tccd` 는 경로를 안 남기고 `fs_usage`/`dtruss` 는 root 가 필요하다(비대화형 세션은 sudo 불가).
**이 경로를 얻기 전에는 어떤 원인 가설도 세우지 마라** — 이 이슈에서 그렇게 세운 가설이 **넷 다 틀렸다.**

### 이 세션이 실험으로 지운 것

| 가설 | 어떻게 지웠나 |
|---|---|
| **TCC 동의 프롬프트가 pending 이라서** | `WORKSPACE_ROOT` 를 바꿔 **NetworkVolumes 요청이 0건**이 되게 만들었는데 **행은 그대로**였다 |
| **작업 디렉터리 위치(OS 임시 폴더)** | 같은 실험 |
| **"워커 안에서 100% 결정적"** | 09-15 12:11 프로세스에서 **12:32 성공 · 12:37 행이 동시에** 있었다 |
| **"TMPDIR 누수가 task 디렉터리"** | `bsvibe-task-*` = **0개**(cleanup 정상). 실제는 `bsvibe-shared-test-*` **17,721개** = **테스트 스위트 누수**(별개 이슈감) |

직전 판의 단서 A·B 도 반증됐다:
* **A (워커 재시작 직후 첫 태스크)** — 19시간 떠 있는 워커에서도 연속으로 행 났다
* **B (#970 커넥터 경고)** — **2.1초에 성공한 셸 replay 의 stderr 에도 그대로** 나왔다

### 누적 배제 목록 (다시 하지 마라)

CLI 자체 · 인증(만료 베어러) · `--model sonnet` · 프롬프트 바이트 replay · launchd 최소 env ·
stdin 미종료 · `MCP_CONNECTION_NONBLOCKING` · CLI 버전(9/12 이후 불변) · uv 버전(2월 이후 불변) ·
**행 난 자식의 env 전량 replay(2.1초 성공)** · **작업 디렉터리 위치** · **TCC**.

⇒ argv·env·cwd·TCC 전부 무죄. **셸에서 replay 하면 항상 성공하고, 워커 안에서만 재현된다.**

### 행 난 프로세스를 심문하는 법 (죽기 전에만 된다)

```bash
lsof -a -p <pid> -i -n -P        # ⚠️ -a 필수. 없으면 AND 가 아니라 OR 다
sample <pid> 5 -f /tmp/s.txt     # 메인 스레드가 어디서 블록하는지
ps -Ewwo command= -p <pid>       # env 전량 — 그대로 복사해 replay 할 수 있다
lsof -a -p <pid> -d cwd -Fn      # cwd
```

---

## §Ⅰ — 이 세션에 머지·배포한 것

| PR | 본질 |
|---|---|
| **#973** | server_sandbox 의 **per-task 임시 디렉터리 제거** — cwd 를 고정 하나로 |
| **#974** | #973 이 만든 회귀 — 테스트가 **형님 실제 홈**에 디렉터리를 남겼다 |

### #973 — 형님 지적에서 나왔다

*"프로젝트는 하나의 디렉터리와 1:1인데, 임시 디렉터리에서 실행될 일이 있어?"*

세어 보니 사용처가 **전량 둘**이었다: subprocess 의 cwd, 그리고 **자기가 만든 걸 지우는 `rmtree`**.
읽는 코드 0, 쓰는 코드 0. `handle_task` 독스트링도 이미 그렇게 말하고 있었다 —
*"The dir exists only because a CLI subprocess needs a cwd."*

**숨은 대가(실측)**: CLI 는 프로젝트 상태를 **cwd 경로로 키잉**한다. 태스크마다 유일한 cwd →
`~/.claude/projects` 에 영구 항목이 하나씩. **994개 중 972개가 `bsvibe-task-*`** 였다.
`rmtree` 는 빈 임시 폴더만 지우고 그 상태는 안 지웠다 — **정리가 깔끔해 보여서 안 보였다.**

⚠️ **클론으로 되돌리지 마라** — E32 에서 이미 해봤다가 되돌렸다(아무도 안 읽는 사본).
진짜 1:1 디렉터리는 **서버 쪽 워크트리**에 있고 에이전트는 MCP work 툴로 거기에 쓴다(T3).

### #974 — 기본값 경로가 사정거리를 바꿨다

#973 이 `$BSVIBE_HOME` 아래 **기본** cwd 를 주고 `makedirs` 하게 되자,
HOME 리다이렉트 픽스처보다 **한 층 밖**에 있던 테스트가 실제 홈에 디렉터리를 남겼다.
픽스처를 `tests/executors/` 로 올리고 가드를 붙였다.

> **교훈**: 기본값 없이 살던 코드에 기본값을 주면, **그때까지 무해했던 호출부가 갑자기 사정거리에 들어온다.**

### 배포 상태
prod 배포 완료 · 워커 3개 재시작(프로세스 시작 시각으로 확인) ·
plist 의 실험용 `BSVIBE_WORKER_WORKSPACE_ROOT` **제거 완료**(#973 이 키 이름을 `SANDBOX_CWD` 로 바꿨다).

---

## §Ⅱ — 다음 작업: "오해를 부르는 코드" 정리 후보

형님 방향 — *"괜히 오해만 불러일으키는 불필요한 코드들은 정리하는 게 좋을 듯"*. #973 이 그 1호다.
**축은 "self-serving 순환 / 없는 것을 주장하는 코드"**, 한 번에 하나씩 PR.

### B. sentry 설정 2개 + `.env.example` 2줄 — **틀린 설정 방법을 알려준다** ← 다음 착수
* `sentry_client_id`/`sentry_client_secret` 참조 **0**
* 그런데 **Sentry 커넥터는 살아 있다**(`connectors/auth/sentry.py`)
* 자격은 **DB 경유**다 — `bootstrap.py:50` `_DB_PROVIDERS` 에 sentry, env 튜플(slack·notion·discord)에는 **없다**
* **그런데 `.env.example` 이 그 셋 바로 아래 나란히 올려놨다** ⇒ 채워도 안 되고 왜 안 되는지 알 길이 없다

### ~~A. 죽은 실행 설정 5개~~ — ✅ 완료
`execution_{prepare,verify,summarize}_round_budget` · `execution_soft_pressure_headroom` ·
`decomposer_cycle_cap` 제거. **대체된 설계의 잔해**였다 — 그 넷이 이름 붙인 단계
(prepare/verify/summarize)는 `backend/workflow/application/` 에 **0건**이고, 현행은 per-run
에이전트 선언(`declare_verification` → `min(declared, ceiling)`)이다. `decomposer_cycle_cap` 주석은
**존재하지 않는 `planning/decomposer.py`** 를 지목하고 있었다.

가드가 일반형으로 남았다 — **`Settings` 의 모든 필드는 선언 외에 읽히는 곳이 있어야 한다**
(`tests/test_every_setting_is_actually_read.py`). 측정: 76개 중 5개 실패, **allowlist 불필요**.
⚠️ 그 가드는 `getattr(settings, "name")` 같은 **동적 접근을 문자열로 잡는다** — 속성 접근만 세면
살아있는 설정 여럿을 죽었다고 오판한다(양성 대조군으로 못 박아 뒀다).

### C. 없는 경로를 가리키는 주석 2건 (수율 낮음 — **일괄 처리 금지**)
* `api/v1/inside/_helpers.py` → `see backend/workers/settle_worker.py` (실제: `backend/knowledge/infrastructure/workers/`)
* `workflow/application/safe_mode_expiry.py` → *"Mirrors `backend/supervisor/audit/events.py`"* (`backend/supervisor/` 가 없다)
* ⚠️ 자동 스캔 10건 중 6건 확인 → **2건만 진짜**. 나머지는 의도적 역사 서술 · 타 레포(BSNexus) 참조 ·
  git SHA 참조 · 프롬프트 안의 예시(`common/mean.py`). 4건 미확인

**A·B 는 설정 삭제라 테스트가 잘 안 잡는다(지워도 초록).** 각각 **부재 가드**를 붙이고,
철자 목록이 아니라 **결과 집합**으로 핀 박고, **전선 절단으로 빨개지는지** 확인할 것.

---

## §Ⅲ — 형님 손에 있는 것

1. **⭐ `sudo fs_usage` 1회** (§0) — 이 한 번이 프레이밍 행 조사를 끝낸다. **최우선**
2. **워커 Claude OAuth 재로그인** — `~/.bsvibe/claude_oauth.json` 이 2026-08-27 만료, refresh 가
   `invalid_grant`. 지금은 CLI 크리덴셜 폴백이 **유일한 생명줄**
3. 이월: **#935 시크릿 로테이션 2건** · **#937 리포 밖** · **Notion 아카이브**

---

## §Ⅳ — 다음 세션 시작점

0. **§0 의 `fs_usage` 측정이 됐는지 확인** — 됐으면 프레이밍 행을 끝내고, 안 됐으면 원인 가설을 세우지 마라
1. **§Ⅱ 의 B → A → C** 순으로 정리 (한 번에 하나씩 PR)
2. **#970 로그 소음** — 5분마다의 트레이스백을 한 줄로 줄이고 백오프. 30분짜리
3. **#957 KMS key-id** · **#964 settle 스코프** · **#928 과금**(단가 근거 없음, **#953 이전 숫자로 튜닝 금지**)

### 검증 안 된 것 (정직하게)
* **#966 체크리스트 3번** — *긴 `act` 턴이 안 잘린다*를 **아직 못 봤다.** 프레이밍이 먼저 죽는다.
  **다음 성공 런에서 반드시** 확인할 것
* **#973 의 E2E 체크리스트**(`docs/e2e/worker-sandbox-cwd-checklist.md`) 중 라이브 항목 —
  프레이밍이 죽어서 **두 번째 런의 cwd 가 같은지**를 실제 런으로 못 봤다(유닛으로는 못 박았다)
* **`bsvibe-shared-test-*` 17,721개** — 테스트 스위트가 TMPDIR 에 흘린다. **별개 이슈로 안 열었다**

---

## §Ⅴ — 이 세션에서 값을 한 규율

* **🚨 상관은 같은 초에 일어나도 인과가 아니다.** TCC 프롬프트가 `executor_turn_started` 와 **같은 초**에
  pending 이었고 두 사건에서 반복됐다. 그래도 틀렸다. 확정은 *"그 조건을 없애면 증상이 사라지는가"*
  로만 온다 — 그 실험이 **코드 0줄·10분**이었는데 **나는 그걸 하기 전에 "원인 확정"이라고 썼다**
  (스킬 `same-second-correlation-is-not-cause-remove-the-condition`)
* **🚨 `log` 는 zsh 빌트인이다.** `log show …` 가 `too many arguments` 로 죽고 **rc=0 으로 0줄**을 뱉는다.
  양성 대조군(전체 줄 수)을 안 셌으면 **"TCC 아님"으로 오판**했다. `/usr/bin/log` 를 써라
* **🚨 `lsof -p PID -i` 는 AND 가 아니라 OR 다.** `-a` 를 넣어라
* **📉 Debug 레벨 로그는 이틀만 남는다**(아카이브가 한 달치여도) — *"이전엔 없었다"* 는
  **보존 한계이지 관측이 아니다**
* **🔬 "재현이 안 된다"는 재현 조건을 아직 못 베꼈다는 뜻** — 셸 replay 가 계속 성공한 건 argv·env 를
  베꼈지 **누가 띄웠는가**를 못 베꼈기 때문이다. **환경을 복사할 때 부모도 환경이다**
* **✂️ "안 쓰인다"의 증명은 grep 한 번이 아니다** — `settings.<name>` 0건이어도 `getattr(settings, name)`
  으로 살아 있는 게 여럿이었다. 그리고 **죽은 설정이 살아있는 주장의 주어**일 수 있다(§Ⅱ.B 의 `.env.example`)
* **🧹 정리가 깔끔해 보이면 무엇을 안 치우는지 본다** — `rmtree` 가 임시 폴더를 지워서, 그게 만든
  **972개의 영구 상태**가 안 보였다
