# 샌드박스 방화벽 실패-폐쇄 — 검증 체크리스트

> 2026-08-30. 감사 **B0**("샌드박스 egress 미격리")를 재측정하다 나왔다.
> 결론은 감사와 다르다: **심각한 절반은 이미 닫혀 있었고**, 진짜 구멍은 그
> 방화벽이 **실패해도 아무도 모른다**는 것이었다.

## 재측정이 감사를 뒤집었다

감사 B0 는 *"에이전트가 돌리는 코드가 네트워크로 자유롭게 나감 → 데이터 유출/
SSRF 벡터"* 라고 적었다. prod 에서 중첩 컨테이너를 띄워 직접 쟀다:

| 프로브 (중첩 샌드박스 → ) | 결과 |
|---|---|
| DinD 제어소켓 `<gw>:2375` (컨테이너 **탈출**) | **BLOCKED** |
| 사설 대역 `10.x` · `172.17.0.1` (postgres·redis·backend = **SSRF**) | **BLOCKED** |
| 링크로컬 `169.254.169.254` (메타데이터) | **BLOCKED** |
| **[양성 대조군]** 공용 `example.com:443` | **OPEN** |

⇒ `deploy/sandbox-dind-firewall.sh` 가 **이미 살아서 돌고 있었다.** 감사가 든 두
위험 중 SSRF·탈출은 닫혀 있다. 공용 egress 는 오작동이 아니라 **의도된 설계**이고
스크립트가 직접 적어 뒀다 — *"leaving the PUBLIC internet OPEN (so `uv sync`/PyPI,
npm, and **founder external-API tasks** keep working)"*.

양성 대조군이 중요하다: 공용이 OPEN 이라는 사실이 **프로브 방법 자체가 작동함**을
증명한다. 그게 없으면 위 BLOCKED 셋은 "프로브가 고장났다"로도 설명된다.

## 그래서 진짜 구멍은 무엇이었나

```sh
apply_firewall &            # ← 백그라운드. 반환값을 아무도 안 읽는다
exec dockerd-entrypoint.sh "$@"
```

* **실패가 조용하다.** `apply_firewall` 이 실패해도(체인 미출현 · iptables 부재 ·
  다른 층이 규칙을 덮음) DinD 는 **격리 없이 정상 기동**하고, 흔적은 stderr 한 줄뿐이다.
  그 상태의 중첩 컨테이너는 전 테넌트 데이터와 컨테이너 탈출에 닿는다.
* **적용을 확인하지 않는다.** `iptables -I` 의 반환값은 *"명령이 접수됐다"*이지
  *"규칙이 지금 테이블에 있다"*가 아니다. 두 명제는 다르다.
* 기존 정적 가드(`test_sandbox_network_isolation.py`)는 스크립트 **내용**만
  고정하며 스스로 한계를 적어 뒀다 — *"CI has no privileged Docker... Live
  enforcement is proven by `docs/e2e/...checklist.md` against the real Mac-Mini."*
  즉 **자동으로 적용 여부를 확인하는 것이 없었다.** 사람이 안 돌리면 몇 주가 간다.

## 고친 것

1. **검증 단계** — 적용 뒤 규칙 5개(사설 4 + INPUT:2375)를 `-C` 로 재확인.
2. **실패-폐쇄** — 실패 시 `kill 1`. 격리를 증명하지 못하는 데몬은 일을 받으면
   안 된다. **조용히 뚫린 채 서비스하느니 죽는 편이 낫다.**
3. **셀프테스트 훅** (`SBX_FW_SELFTEST=1`) — privileged Docker 없이도 CI 가
   **진짜 실행**으로 검증한다. 가짜 `iptables`/`ip` 를 PATH 앞에 놓고 종료코드로
   판정하므로, 철자만 맞고 동작이 틀린 구현은 통과 못 한다.

## 체크리스트

- [x] 정상 경로 exit 0 **(양성 대조군 — 없으면 실패 테스트들이 "늘 실패"로도 통과)**
- [x] `-I` 는 성공하는데 규칙이 없으면 **실패** ← 이 PR 의 핵심
- [x] `DOCKER-USER` 체인 미출현 시 실패
- [x] `docker0` 미출현 시 실패
- [x] 실패 경로가 PID 1 을 죽인다
- [x] `verify_firewall` 이 정의만 되고 방치되지 않는다(호출 경로에 있다)
- [x] **음성 대조군 4층 — 각 층이 자기 테스트만 떨어뜨린다** (검증 제거 · `kill 1`
      제거 · 브리지 대기 무시 · 체인 대기 무시). 알리바이 0건
- [x] 기존 정적 가드 15건이 그대로 통과 — 고정된 불변식(사설 대역 4 · 체인 ·
      엔트리포인트 래핑 · 외부 인터페이스 무간섭)을 안 깼다
- [x] **prod DinD 실환경 실행** (§아래)
- [x] 전체 게이트 (fresh PG · pytest+cov80 · ruff · format · mypy · lint-imports)

## prod 실증 (2026-08-30, `bsvibe-sandbox-dind`)

```
① 진짜 iptables (규칙 존재)
   [sbx-fw] nested-sandbox egress isolation applied (...)
   [sbx-fw] verified: every isolation rule is present
   exit=0

② -C 가 항상 실패하는 가짜 iptables
   [sbx-fw] nested-sandbox egress isolation applied (...)   ← 여전히 "applied" 라고 말한다
   [sbx-fw] FATAL: rules absent after apply — FORWARD:10.0.0.0/8 FORWARD:172.16.0.0/12
            FORWARD:192.168.0.0/16 FORWARD:169.254.0.0/16 INPUT:2375
   exit=1
```

②가 이 PR 전체를 한 화면에 보여준다: **`apply_firewall` 은 여전히 "applied" 라고
말하는데 검증이 그 거짓말을 잡는다.** 이전에는 저 상태로 데몬이 떴다.

## ⚠️ 이 PR 이 만든 알리바이를 스스로 잡았다

`kill 1` 을 검사하는 첫 테스트는 `"kill" in script.lower()` 였고, 음성 대조군에서
`kill 1` → `true` 로 바꿔도 **통과했다**. 내가 헤더에 쓴 설명 문장
*"On any failure, KILL PID 1"* 이 grep 을 만족시켰기 때문이다 — **가드가 자기
산문에 걸렸다.**

주석 줄을 걷어내고 실행되는 줄만 세도록 바꾸자 정상적으로 떨어졌다.
[[absence-guard-listing-spellings-proves-only-imagination]] 의 거울상이다:
부재를 주장할 때 산문이 가드를 **빨갛게** 만들듯, 존재를 주장할 때 산문은 가드를
**초록으로** 만든다. 어느 쪽이든 **산문은 후보가 아니어야 한다.**

## 남는 것 — 형님 판단으로 하지 않은 것

**공용 인터넷 egress 는 열어 둔다.** allowlist 프록시를 걸면 패키지 설치만이
아니라 *에이전트가 임의 외부 API 를 호출하는 제품 기능* 자체가 막힌다(방화벽
스크립트가 그 요구를 명시적으로 적어 뒀다). 보안 강화이면서 동시에 기능 축소
결정이므로 형님이 **가드만** 만들기로 정했다 (2026-08-30).

**DinD 네트워크 누수 (별건).** 3일 전 죽은 런의 `sbxnet-6c19a033…` 이 남아 있다 —
컨테이너가 exit 255 로 죽어 `_teardown` 경로를 안 탔다. 실행마다 새는 구조.
