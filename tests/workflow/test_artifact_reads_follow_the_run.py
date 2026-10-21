"""#929 (B) — a deliverable's file is served whichever machine answers.

``read_deliverable_artifact`` used to try the run's own directory, then the product's ``main``.
With more than one machine the API answering may not hold the run's tree: a run waiting for
review lives on its home machine, with its latest state checkpointed. So between the two:

1. this machine's run tree (the run is here),
2. the run's checkpoint (the run is on another machine, or between machines),
3. the product's ``main`` (the run shipped and its tree was removed).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from backend.workflow.application import deliverable_artifact as module
from backend.workflow.application.deliverable_artifact import read_deliverable_artifact


class _Store:
    def __init__(self, raw: bytes | None) -> None:
        self.raw = raw

    def read_bytes(self, run_id: uuid.UUID, ref: str) -> bytes:
        if self.raw is None:
            raise FileNotFoundError(ref)
        return self.raw


class _Deliverables:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def get(self, _id: uuid.UUID) -> Any:
        return self.row


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"checkpoint": None, "main": None, "asked": []}

    async def _checkpoint(run_id: uuid.UUID, ref: str) -> bytes | None:
        state["asked"].append("checkpoint")
        return state["checkpoint"]

    async def _main(session: Any, run_id: uuid.UUID, ref: str) -> bytes | None:
        state["asked"].append("main")
        return state["main"]

    monkeypatch.setattr(module, "read_run_checkpoint_file", _checkpoint)
    monkeypatch.setattr(module, "_read_from_product_main", _main)
    return state


async def _read(local: bytes | None) -> Any:
    ws = uuid.uuid4()
    row = SimpleNamespace(workspace_id=ws, run_id=uuid.uuid4(), payload={"artifact_refs": ["a.py"]})
    return await read_deliverable_artifact(
        deliverable_id=uuid.uuid4(),
        ref="a.py",
        workspace_id=ws,
        session=None,  # type: ignore[arg-type]
        store=_Store(local),  # type: ignore[arg-type]
        deliverables=_Deliverables(row),  # type: ignore[arg-type]
    )


async def test_the_local_tree_wins(sources) -> None:
    sources["checkpoint"], sources["main"] = b"checkpoint", b"main"

    assert (await _read(b"local")).content == "local"
    assert sources["asked"] == []


async def test_a_run_elsewhere_is_read_from_its_checkpoint(sources) -> None:
    sources["checkpoint"], sources["main"] = b"latest work", b"older main"

    assert (await _read(None)).content == "latest work"
    assert sources["asked"] == ["checkpoint"]


async def test_a_shipped_run_falls_back_to_the_product_main(sources) -> None:
    sources["main"] = b"shipped"

    assert (await _read(None)).content == "shipped"
    assert sources["asked"] == ["checkpoint", "main"]


async def test_gone_everywhere_is_a_calm_not_found(sources) -> None:
    assert await _read(None) is None
