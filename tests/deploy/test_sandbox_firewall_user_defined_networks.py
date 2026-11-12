"""방화벽 규칙이 사용자 정의 네트워크(``sbxnet-*`` → ``br-<id>``)에도 걸린다.

가짜 ``iptables`` 가 받은 인자를 로그에 남기고, 셀프테스트 모드로 실제 스크립트를
돌려 *생성되는 규칙 목록*을 검사한다.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "sandbox-dind-firewall.sh"
_PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]


def _run_and_collect(tmp_path: Path) -> list[str]:
    log = tmp_path / "rules.log"
    for name, body in {
        # -C 는 항상 "없음", -I 는 로그 후 성공 → 삽입 규칙 목록이 그대로 남는다.
        "iptables": (
            f'case "$*" in *"-L DOCKER-USER"*) exit 0 ;; '
            f'*"-C "*) exit 1 ;; *"-I "*) echo "$*" >> {log}; exit 0 ;; esac\nexit 0\n'
        ),
        "ip": "exit 0\n",
    }.items():
        p = tmp_path / name
        p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SBX_FW_SELFTEST": "1",
        "SBX_FW_WAIT_TRIES": "2",
    }
    # -C 가 항상 실패하므로 verify 는 실패한다 — 여기선 삽입된 규칙만 본다.
    subprocess.run(["/bin/sh", str(_SCRIPT)], env=env, capture_output=True, timeout=60)
    return log.read_text(encoding="utf-8").splitlines()


def test_rules_cover_user_defined_networks(tmp_path: Path) -> None:
    rules = _run_and_collect(tmp_path)
    for iface in ("docker0", "br+"):
        for cidr in _PRIVATE:
            assert any(f"DOCKER-USER -i {iface} -d {cidr} -j DROP" in r for r in rules), (
                f"{iface} → {cidr} DROP 규칙이 없다"
            )
        assert any(f"INPUT -i {iface} -p tcp --dport 2375 -j DROP" in r for r in rules), (
            f"{iface} 의 2375 차단 규칙이 없다"
        )


def test_same_network_traffic_is_returned_before_the_drops(tmp_path: Path) -> None:
    rules = _run_and_collect(tmp_path)
    # -I 로 맨 앞에 넣으므로 가장 마지막에 삽입된 규칙이 먼저 평가된다.
    assert "physdev --physdev-is-bridged -j RETURN" in rules[-1]


def test_verify_fails_when_only_docker0_rules_exist(tmp_path: Path) -> None:
    """br+ 규칙만 빠져도 DinD 가 죽어야 한다(fail-closed)."""
    body = 'case "$*" in *"-L DOCKER-USER"*) exit 0 ;; *"-C "*"br+"*) exit 1 ;; esac\nexit 0\n'
    p = tmp_path / "iptables"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    q = tmp_path / "ip"
    q.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    q.chmod(q.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SBX_FW_SELFTEST": "1",
        "SBX_FW_WAIT_TRIES": "2",
    }
    r = subprocess.run(["/bin/sh", str(_SCRIPT)], env=env, capture_output=True, timeout=60)
    assert r.returncode != 0
