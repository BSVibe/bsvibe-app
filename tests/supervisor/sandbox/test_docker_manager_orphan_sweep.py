"""고아 샌드박스/네트워크를 데몬 상태에서 쓸어담는다 — 인메모리 맵이 아니라.

## 실측 (2026-08-30, prod DinD)

    bsvibe-sbx-6c19a033…        Exited (255)  4 days ago
    bsvibe-sbx-pg-6c19a033…     Exited (0)    4 days ago
    bsvibe-sbx-35f71984…        Exited (255)  6 weeks ago
    sbxnet-6c19a033…            네트워크 생존, 붙은 컨테이너 0

## 근본 원인

``reap_idle()`` 은 ``self._containers`` — **인메모리 dict** — 만 순회한다.
워커 프로세스가 재시작하면 그 맵이 비고, 그 시점에 살아 있던 컨테이너·사이드카·
네트워크는 **영원히 도달 불가능한 고아**가 된다. 6주 된 컨테이너가 그 증거다.
(이 세션에서만 워커를 3번 kickstart 했다.)

## 왜 "안 도는 것만" 쓸어담나

한 DinD 를 여러 워커 프로세스가 공유할 수 있다. *"내 맵에 없으면 남의 것"* 으로
지우면 **다른 프로세스의 살아 있는 샌드박스를 죽인다.** running 컨테이너는 건드리지
않고, 종료된 것과 **붙은 컨테이너가 0인 네트워크**만 지우면 그 위험이 사라진다 —
살아 있는 작업은 정의상 running 이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from backend.workflow.infrastructure.sandbox import DockerSandboxManager


@dataclass
class _Daemon:
    """``docker ps -a`` / ``network ls`` / ``network inspect`` 를 흉내낸다."""

    #: 이름 → running 여부
    containers: dict[str, bool] = field(default_factory=dict)
    #: 네트워크 이름 → 붙은 컨테이너 수
    networks: dict[str, int] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    async def __call__(self, argv, *, timeout_s, stdin=None):
        self.calls.append(argv)
        sub = argv[0]
        if sub == "version":
            return (0, b"24.0.0\n", b"")
        if sub == "ps":
            lines = [f"{n}\t{'true' if r else 'false'}" for n, r in sorted(self.containers.items())]
            return (0, ("\n".join(lines) + "\n").encode(), b"")
        if sub == "network":
            if argv[1] == "ls":
                return (0, ("\n".join(sorted(self.networks)) + "\n").encode(), b"")
            if argv[1] == "inspect":
                return (0, str(self.networks.get(argv[-1], 0)).encode() + b"\n", b"")
            if argv[1] == "rm":
                self.networks.pop(argv[-1], None)
                return (0, b"", b"")
        if sub == "rm":
            self.containers.pop(argv[-1], None)
            return (0, b"", b"")
        if sub == "inspect":
            return (0, b"true\n" if self.containers.get(argv[-1]) else b"false\n", b"")
        return (0, b"", b"")


def _mgr(monkeypatch, daemon: _Daemon) -> DockerSandboxManager:
    m = DockerSandboxManager(
        docker_host="tcp://x:2375",
        sandbox_image="img",
        idle_reap_seconds=60,
        max_concurrent=2,
    )
    monkeypatch.setattr(m, "_docker", daemon)
    return m


@pytest.mark.asyncio
async def test_it_reaps_exited_sandboxes_no_live_process_remembers(monkeypatch) -> None:
    """**핵심** — 인메모리 맵이 비어 있어도 데몬 상태에서 찾아 지운다.

    이게 프로세스 재시작 후의 상태다. 앞 세대 reaper 는 여기서 아무것도 못 했다.
    """
    d = _Daemon(
        containers={
            "bsvibe-sbx-aaa": False,
            "bsvibe-sbx-pg-aaa": False,
            "bsvibe-sbx-bbb": True,  # 살아 있음 — 건드리면 안 된다
        },
        networks={"sbxnet-aaa": 0, "sbxnet-bbb": 1},
    )
    m = _mgr(monkeypatch, d)
    assert m._containers == {}  # 재시작 직후

    await m.sweep_orphans()

    assert "bsvibe-sbx-aaa" not in d.containers
    assert "bsvibe-sbx-pg-aaa" not in d.containers
    assert "sbxnet-aaa" not in d.networks


@pytest.mark.asyncio
async def test_it_never_touches_a_running_sandbox(monkeypatch) -> None:
    """⭐ 동시성 안전 — 다른 워커 프로세스의 살아 있는 작업을 죽이면 안 된다.

    한 DinD 를 여러 프로세스가 공유할 수 있으므로 *"내 맵에 없으면 고아"* 는
    쓸 수 없는 기준이다. running 이면 남의 살아 있는 작업이다.
    """
    d = _Daemon(containers={"bsvibe-sbx-bbb": True}, networks={"sbxnet-bbb": 1})
    m = _mgr(monkeypatch, d)

    await m.sweep_orphans()

    assert d.containers == {"bsvibe-sbx-bbb": True}
    assert d.networks == {"sbxnet-bbb": 1}


@pytest.mark.asyncio
async def test_it_never_touches_foreign_names(monkeypatch) -> None:
    """양성 대조군 — 접두사 밖의 것은 이 매니저 소유가 아니다."""
    d = _Daemon(
        containers={"someone-elses-pg": False, "bsvibe-sbx-aaa": False},
        networks={"unrelated-net": 0, "sbxnet-aaa": 0},
    )
    m = _mgr(monkeypatch, d)

    await m.sweep_orphans()

    assert "someone-elses-pg" in d.containers
    assert "unrelated-net" in d.networks
    assert "bsvibe-sbx-aaa" not in d.containers


@pytest.mark.asyncio
async def test_a_settled_daemon_is_a_no_op(monkeypatch) -> None:
    """양성 대조군 — 지울 게 없으면 아무 rm 도 내지 않는다(멱등)."""
    d = _Daemon(containers={"bsvibe-sbx-bbb": True}, networks={"sbxnet-bbb": 1})
    m = _mgr(monkeypatch, d)

    await m.sweep_orphans()

    assert not [c for c in d.calls if c[0] == "rm" or c[:2] == ["network", "rm"]]


@pytest.mark.asyncio
async def test_the_sweep_is_actually_wired_to_daemon_readiness(monkeypatch) -> None:
    """⭐ 배선 가드 — 이 세션이 계속 지워온 "호출자 0" 결함을 내가 만들지 않도록.

    구현만 있고 아무도 안 부르면 스윕은 존재하지 않는 것과 같다. 그리고 프로세스당
    1회여야 한다 — 매 sandbox 생성마다 데몬 전체를 훑을 이유가 없다.
    """
    d = _Daemon(containers={"bsvibe-sbx-aaa": False}, networks={})
    m = _mgr(monkeypatch, d)
    seen: list[int] = []
    orig = m.sweep_orphans

    async def _counted() -> None:
        seen.append(1)
        await orig()

    monkeypatch.setattr(m, "sweep_orphans", _counted)

    await m._await_dind()
    await m._await_dind()

    assert seen == [1], f"스윕이 배선되지 않았거나 매번 돈다: {len(seen)}회"
