"""#929 (B) — the verification disk bound is per MACHINE, not per cluster.

``total_slots`` exists because a full disk on the box that runs the stack is an unrecoverable
brick: it is a property of one machine's disk. The global slot key had no machine axis, so
with two machines it would have acted cluster-wide — box B refused a stack because box A's
disk was busy, and a cluster of N boxes could hold only one box's worth of stacks.

``BSVIBE_MACHINE_ID`` names the machine. Empty (the single-box default) keeps every key
byte-for-byte what it was, so a running deployment does not change behaviour.

It is NOT the container hostname: the backend and the worker on one box have different
hostnames, and keying on that would hand each container its own disk budget on one disk.
"""

from __future__ import annotations

import hashlib
import inspect
import uuid

from backend.workflow.infrastructure.verify_slots import acquire_verify_slot, verify_slot_key

from .._support import memory_session


def _pre_929_key(index: int) -> int:
    digest = hashlib.blake2b(
        b"bsvibe.verify.slot/" + index.to_bytes(4, "big", signed=False), digest_size=8
    ).digest()
    unsigned = int.from_bytes(digest, byteorder="big", signed=False)
    return unsigned - 2**64 if unsigned >= 2**63 else unsigned


def test_a_single_box_keeps_its_keys() -> None:
    assert [verify_slot_key(i) for i in range(4)] == [_pre_929_key(i) for i in range(4)]
    assert [verify_slot_key(i, machine="") for i in range(4)] == [_pre_929_key(i) for i in range(4)]


def test_machines_get_disjoint_stable_keys() -> None:
    a = {verify_slot_key(i, machine="box-a") for i in range(8)}
    b = {verify_slot_key(i, machine="box-b") for i in range(8)}

    assert not a & b
    assert not a & {_pre_929_key(i) for i in range(8)}
    assert verify_slot_key(0, machine="box-a") == verify_slot_key(0, machine="box-a")


async def test_each_machine_has_its_own_disk_bound() -> None:
    async with memory_session() as s1, memory_session() as s2, memory_session() as s3:
        async with acquire_verify_slot(
            s1, workspace_id=uuid.uuid4(), slots=1, total_slots=1, machine="box-a"
        ) as a:
            async with acquire_verify_slot(
                s2, workspace_id=uuid.uuid4(), slots=1, total_slots=1, machine="box-b"
            ) as b:
                async with acquire_verify_slot(
                    s3, workspace_id=uuid.uuid4(), slots=1, total_slots=1, machine="box-a"
                ) as a2:
                    assert a is not None and b is not None, "box B is not full because A is"
                    assert a2 is None, "box A's own disk bound still holds"


def test_the_verifier_passes_this_machines_id() -> None:
    from backend.workflow.application import verify_environment

    src = "".join(inspect.getsource(verify_environment.open_run_check_environment).split())
    assert "machine=settings.machine_id" in src


async def test_a_released_machine_slot_can_be_taken_again() -> None:
    """Release must give back the SAME machine-scoped key it took — a mismatch leaks the slot
    and the box never verifies again."""
    async with memory_session() as s1, memory_session() as s2:
        async with acquire_verify_slot(
            s1, workspace_id=uuid.uuid4(), slots=1, total_slots=1, machine="box-a"
        ) as first:
            assert first is not None
        async with acquire_verify_slot(
            s2, workspace_id=uuid.uuid4(), slots=1, total_slots=1, machine="box-a"
        ) as again:
            assert again is not None


def test_two_servers_never_share_a_compose_project() -> None:
    """A client_attach run's stack runs on the founder's machine — the SAME docker for every
    server driving that founder's runs. With ``verify-slot-0`` on both servers, the second
    server's pre-clear ``down`` would tear down the first one's live stack (#929)."""
    from backend.workflow.infrastructure.verify_slots import VerifySlot, verify_project_name

    assert verify_project_name(0) == "verify-slot-0", "a single box is unchanged"
    assert verify_project_name(0, machine="box-a") != verify_project_name(0, machine="box-b")
    assert verify_project_name(0, machine="Box_A.local") == "verify-slot-box_a-local-0", (
        "compose project names are lowercase letters, digits, - and _"
    )
    assert VerifySlot(index=1, machine="box-a").project == "verify-slot-box-a-1"


async def test_the_held_slot_names_its_machines_project() -> None:
    async with memory_session() as s1, memory_session() as s2:
        async with acquire_verify_slot(
            s1, workspace_id=uuid.uuid4(), slots=1, total_slots=1, machine="box-a"
        ) as a:
            async with acquire_verify_slot(
                s2, workspace_id=uuid.uuid4(), slots=1, total_slots=1, machine="box-b"
            ) as b:
                assert a is not None and b is not None
                assert (a.project, b.project) == ("verify-slot-box-a-0", "verify-slot-box-b-0")
