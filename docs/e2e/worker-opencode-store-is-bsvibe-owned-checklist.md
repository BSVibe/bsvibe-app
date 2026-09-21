# E2E — 워커의 opencode 스토어가 BSVibe 소유다 (#1016)

**대상 PR**: `opencode serve` 를 `OPENCODE_DB` 로 **워커별 BSVibe 경로**에 고정
**전제**: 호스트 워커는 **autodeploy 안 된다.** 배포마다
`launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` 후 **프로세스 시작 시각**으로 확인.
(plist 는 안 바뀌므로 `bootout`+`bootstrap` 은 불필요 — 코드만 바뀐다)

> 🧭 **인수인계와 이슈가 지목한 손잡이는 틀렸다.** `WorkerSettings.opencode_data_dir`
> 은 **데몬을 조종하지 않는다** — `opencode_data_dir()` 는 격리/로그가 *찾아볼* 경로를
> 유도할 뿐이고, 데몬은 `sanitized_subprocess_env()` 를 그대로 받는다. 그 값만 바꿨다면
> **워커는 A 를 격리하고 opencode 는 B 에 쓰는 split-brain** 이 됐을 것이다.

---

## 사전 측정 — 어떤 손잡이가 실재하나 (실측, 2026-09-21)

- [x] 바이너리(1.17.3)가 **실제로 읽는** 환경변수: `XDG_DATA_HOME` · **`OPENCODE_DB`** · `OPENCODE_CONFIG_DIR`
- [x] 🚨 **`XDG_DATA_HOME` 는 쓰면 안 된다** — 데이터 디렉터리 **통째로** 옮기는데
      거기 **`auth.json`(프로바이더 크리덴셜)** 이 산다. 스토어를 가르면서 워커 로그인을
      잃는 건 고침이 아니다
- [x] **`OPENCODE_DB` 는 존중된다** — 절대경로를 주니 그 자리에 `worker.db` + `-wal`/`-shm` 생성
- [x] ⭐ **값이 원인임을 대조군으로** — 값을 `other-name.db` 로 바꾸니 파일도 따라갔다
      (cwd 유도도, 고정 경로도 아니다)
- [x] 🚨⭐ **공유 스토어 무변화** — 프로브 2회 전후로 `opencode.db`/`-wal`/`-shm`
      **mtime·크기 전부 동일**. 지난 세션에 prod 를 멈춘 그 경로를 이번엔 안 밟았다
      (워커 데몬 pid 58439 가 살아 있는 채로 쟀다)
- [x] ⚠️ **내 탐지기가 거짓 경보를 냈다** — 2차 스냅샷에서 glob 이 `.bak-*` 까지 잡아
      파일 집합이 달라졌고 diff 가 🚨를 찍었다. 같은 세 파일로 다시 재니 무변화.
      *비교하는 두 스냅샷의 대상 집합이 같은지부터 봐라.*
- [x] `BSVIBE_WORKER_NAME` 이 plist 마다 주입된다(`admin-test-exec`) ⇒ **워커별 축이 존재한다**

## 유닛 — `tests/executors/worker/test_opencode_store_is_bsvibe_owned.py` (8개)

- [x] 데몬이 **BSVibe 소유 절대경로**로 고정된다 · `~/.local/share/opencode` 가 아니다
- [x] **워커별로 다르다**(한 호스트의 두 데몬이 같은 SQLite 를 물지 않는다)
- [x] 🔐 **이름이 루트를 탈출 못 한다** — `BSVIBE_WORKER_NAME=../../../etc/evil` 이 경로에 닿는다
- [x] 명시 설정(`opencode_db_path`)이 이긴다 · 부모 디렉터리가 생성된다
- [x] ⭐ **`XDG_DATA_HOME` 는 건드리지 않는다** — 설계 결정 자체를 가드로 박았다
- [x] 복구(corruption)가 **우리가 고정한 스토어**를 격리한다 ·
      **형님 스토어는 바이트 그대로** 남는다
- [x] **기본값**이 공유 경로가 아니다 (설정 안 한 상태가 prod 가 도는 상태다)

## 전선 절단 — 5/5, 전부 컴파일됨

| 끊은 것 | 빨개진 것 |
|---|---|
| `OPENCODE_DB` 주입 | **5** (고정 경로 계열 전부) |
| 경로의 워커 이름 축 | 1 — `test_the_store_is_per_worker` |
| 이름 살균 | 1 — `test_a_worker_name_cannot_escape_the_bsvibe_root` |
| 복구 대상을 옛 데이터 디렉터리로 되돌림 | 1 — `test_corruption_recovery_quarantines_the_worker_store` |
| ⭐ **`XDG_DATA_HOME` 도 같이 설정** | 1 — `test_the_provider_credential_is_NOT_moved` (**가드가 뒤집힌다**) |

전체 스위트 **6424 passed** · import-linter 6/6 · ruff · mypy 깨끗.

## 배포 후 — prod 실측

- [ ] 배포 확인: prod SHA + 컨테이너 재생성 시각
- [ ] 워커 재시작: `launchctl kickstart -k gui/501/com.bsvibe.worker-mac-mini-e2e`
      → **프로세스 시작 시각**으로 확인
- [ ] 워커 로그의 `opencode_serve_starting` 에 **`db_path=~/.bsvibe/opencode/<worker>/opencode.db`**
- [ ] 그 경로에 `opencode.db` 가 **실제로 생겼는가** · `~/.local/share/opencode/opencode.db` 의
      **mtime 이 안 움직였는가** (같은 세 파일로 비교할 것 — 위의 거짓 경보 참고)
- [ ] opencode 런 하나가 `review_ready` 까지 간다 (인증이 안 깨졌다는 증거 = `auth.json` 공유가 통한다)
- [ ] ⭐ **양성 대조군**: 호스트 셸에서 `opencode` 를 띄워 둔 채 워커 런을 돌려 **멀쩡한지** 본다.
      이게 이 이슈의 진짜 수용 조건이다 — 지금은 형님이 띄우기만 해도 워커가 멈춘다
- [ ] admin 워커의 `opencode_serve_startup_failed` 가 달라지는지 확인(#970 로그 노이즈와 겹친다)

## 안 잰 것 / 이월

- [ ] **임시 조치를 되돌릴지**: 워커 plist PATH 앞의 `~/.opencode/bin`(1.17.3 고정)은
      스토어가 갈린 지금 **더 이상 필요 없을 수 있다.** 다만 #1014 의 측정이 1.17.3 기준이라
      버전을 바꾸는 건 별건이다. **이 PR 은 plist 를 건드리지 않는다**
- [ ] `log` 디렉터리와 `auth.json` 은 **여전히 공유**다(의도). 로그가 섞이는 것의 실害는 안 쟀다
- [ ] 기존 공유 스토어에 남은 워커 세션 이력은 **버려진다**(새 스토어가 빈 채로 시작). 복구 안 한다
