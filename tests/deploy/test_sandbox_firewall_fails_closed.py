"""방화벽이 적용에 실패하면 DinD 가 **죽어야 한다** — 조용히 뚫린 채 뜨면 안 된다.

## 왜 이 가드가 필요한가 (2026-08-30 실측)

``sandbox-dind-firewall.sh`` 는 중첩 샌드박스의 egress 를 격리한다 — 사설 대역
전부 DROP(내부 SSRF 차단) + 데몬 제어소켓 ``:2375`` DROP(컨테이너 탈출 차단).
prod 에서 **실제로 살아 있음을 확인했다**(중첩 컨테이너에서 프로브: 사설·링크로컬·
2375 전부 BLOCKED, 양성 대조군으로 공용 ``example.com:443`` 은 OPEN).

그런데 적용 경로가 이렇게 생겼다:

    apply_firewall &            # ← 백그라운드. 반환값을 아무도 안 읽는다
    exec dockerd-entrypoint.sh "$@"

**``apply_firewall`` 이 실패해도 DinD 는 정상 기동한다.** 120초 타임아웃 FATAL 도
stderr 한 줄로 끝나고, 데몬은 격리 **없이** 중첩 컨테이너를 받기 시작한다. 그
상태에서 에이전트 코드는 postgres·redis·backend 와 ``:2375`` 에 닿는다 —
전 테넌트 데이터와 컨테이너 탈출.

그리고 적용 뒤 **규칙이 실제로 들어갔는지 확인하지 않는다.** ``iptables -I`` 가
성공을 반환해도 다른 층이 나중에 규칙을 뒤엎을 수 있고, 그때도 아무 신호가 없다.

## 왜 정적 grep 가드로는 부족한가

`test_sandbox_network_isolation.py` 가 이미 스크립트 **내용**을 촘촘히 고정한다
(사설 대역 4개 · ``DOCKER-USER`` 체인 · 엔트리포인트 래핑 · 외부 인터페이스
무간섭). 그 파일은 자기 한계를 이렇게 적어 뒀다 — *"CI has no privileged
Docker... Live enforcement is proven by docs/e2e/...checklist.md against the
real Mac-Mini."*

즉 **적용 여부를 자동으로 확인하는 것이 없다.** 사람이 체크리스트를 손으로
돌려야 하고, 안 돌리면 조용히 뚫린 채 몇 주가 간다.

여기서는 privileged Docker 없이도 **진짜 동작**을 검증한다: 스크립트에 셀프테스트
모드를 두고 가짜 ``iptables`` / ``ip`` 를 PATH 에 놓아 **실제로 실행**한다. 정적
grep 이 아니라 종료코드로 판정하므로, 철자만 맞고 동작이 틀린 구현은 통과 못 한다.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "deploy" / "sandbox-dind-firewall.sh"

#: 셀프테스트 모드 — 이 환경변수가 켜지면 스크립트는 방화벽만 적용/검증하고
#: ``dockerd-entrypoint.sh`` 를 exec 하지 않은 채 종료코드로 결과를 낸다.
_SELFTEST_ENV = "SBX_FW_SELFTEST"


def _fake_bin(tmp_path: Path, name: str, body: str) -> None:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp_path: Path, *, iptables_body: str, ip_body: str = "exit 0"
) -> subprocess.CompletedProcess:
    """스크립트를 셀프테스트 모드로 실제 실행한다 — 가짜 도구를 PATH 앞에 두고."""
    _fake_bin(tmp_path, "iptables", iptables_body)
    _fake_bin(tmp_path, "ip", ip_body)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        _SELFTEST_ENV: "1",
        # 대기 루프가 테스트를 붙잡지 않도록.
        "SBX_FW_WAIT_TRIES": "2",
    }
    return subprocess.run(
        ["/bin/sh", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


#: 모든 규칙이 이미 존재한다고 답하는 iptables — ``-C`` 성공, ``-I`` 성공.
_IPTABLES_ALL_PRESENT = 'case "$*" in *"-L DOCKER-USER"*) exit 0 ;; esac\nexit 0\n'

#: ``-C``(존재 확인)는 항상 실패하고 ``-I``(삽입)는 성공한다고 답한다 — 즉
#: "넣었다고 하는데 실제로는 없는" 상태. 검증 단계가 없으면 이걸 못 잡는다.
_IPTABLES_INSERT_LIES = """
case "$*" in
  *"-L DOCKER-USER"*) exit 0 ;;
  *"-C "*) exit 1 ;;
  *"-I "*) exit 0 ;;
esac
exit 0
"""


def test_the_script_supports_being_run_without_execing_dockerd() -> None:
    """셀프테스트 훅이 있어야 CI 가 privileged Docker 없이 동작을 검증한다."""
    assert _SELFTEST_ENV in _SCRIPT.read_text(encoding="utf-8")


def test_it_succeeds_when_every_rule_lands(tmp_path: Path) -> None:
    """양성 대조군 — 정상 경로에서 0을 내야 한다.

    이게 없으면 아래 실패 테스트들이 "스크립트가 늘 실패한다"로도 통과한다.
    """
    r = _run(tmp_path, iptables_body=_IPTABLES_ALL_PRESENT)
    assert r.returncode == 0, f"정상 경로가 실패했다:\n{r.stderr}"


def test_it_fails_when_the_rules_are_not_actually_present(tmp_path: Path) -> None:
    """**삽입이 성공을 반환해도 규칙이 없으면 실패해야 한다.**

    적용과 확인은 다른 명제다. ``-I`` 의 반환값은 "명령이 접수됐다"이지
    "규칙이 지금 거기 있다"가 아니다.
    """
    r = _run(tmp_path, iptables_body=_IPTABLES_INSERT_LIES)
    assert r.returncode != 0, "규칙이 없는데 성공을 냈다 — 검증 단계가 없다"


def test_it_fails_when_the_docker_user_chain_never_appears(tmp_path: Path) -> None:
    """체인이 안 생기면(다른 dockerd, iptables 비활성) 실패해야 한다."""
    r = _run(tmp_path, iptables_body='case "$*" in *"-L DOCKER-USER"*) exit 1 ;; esac\nexit 0\n')
    assert r.returncode != 0, "DOCKER-USER 체인이 없는데 성공을 냈다"


def test_it_fails_when_the_nested_bridge_never_appears(tmp_path: Path) -> None:
    """``docker0`` 이 안 뜨면 실패해야 한다 — 걸 곳이 없다."""
    r = _run(tmp_path, iptables_body=_IPTABLES_ALL_PRESENT, ip_body="exit 1")
    assert r.returncode != 0, "브리지가 없는데 성공을 냈다"


def _code_only(text: str) -> str:
    """주석을 걷어낸 코드만 낸다.

    첫 판은 ``"kill" in script.lower()`` 였고 **알리바이였다**: ``kill 1`` 을
    ``true`` 로 바꿔도 통과했다 — 내가 헤더에 쓴 *"On any failure, KILL PID 1"*
    이라는 **설명 문장**이 grep 을 만족시켰기 때문이다. 가드가 자기 산문에
    걸린 것이고, 이건 "본 적 있는 철자를 나열하는" 부재 가드의 거울상이다.

    그래서 산문을 후보에서 뺀다 — 실행되는 줄만 센다.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_the_guard_verifies_and_does_not_merely_apply() -> None:
    """적용과 확인은 다른 명제다 — 둘 다 **실행 경로에** 있어야 한다."""
    code = _code_only(_SCRIPT.read_text(encoding="utf-8"))
    # 정의 1회 + 호출 1회 이상. 정의만 있고 아무도 안 부르면 죽은 함수다.
    assert code.count("verify_firewall") >= 2, "verify_firewall 이 호출되지 않는다"


def test_the_failure_path_kills_pid_1() -> None:
    """조용히 뚫린 채 서비스하느니 죽는 편이 낫다.

    백그라운드로 던진 ``apply_firewall &`` 의 반환값은 구조적으로 아무도 안
    읽는다. 실패를 **컨테이너 종료**로 승격하지 않으면 그 실패는 존재하지
    않는 것과 같다.
    """
    code = _code_only(_SCRIPT.read_text(encoding="utf-8"))
    assert "kill 1" in code, "실패 경로가 PID 1 을 죽이지 않는다"
