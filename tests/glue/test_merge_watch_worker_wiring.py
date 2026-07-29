"""PR4 — MergeWatchWorker is wired into the worker runtime ONLY when opted in.

``build_worker_runtime`` appends the ``merge_watch_worker`` to the worker set
iff ``settings.github_auto_merge_enabled`` is True — off means it is not in the
list at all (so ``github_merge_watch`` is never polled).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.config import get_settings
from backend.workflow.application.runtime import build_worker_runtime

pytestmark = pytest.mark.asyncio


def _names(settings) -> set[str]:  # noqa: ANN001
    runtime = build_worker_runtime(
        session_factory=MagicMock(),
        execution=MagicMock(),
        delivery_adapter=MagicMock(),
        notify_sender=MagicMock(),
        settings=settings,
    )
    return {getattr(w, "_name", None) for w in runtime.workers}


async def test_merge_watch_worker_absent_when_flag_off() -> None:
    settings = get_settings().model_copy(update={"github_auto_merge_enabled": False})
    assert "merge_watch_worker" not in _names(settings)


async def test_merge_watch_worker_present_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gated construction builds a CredentialCipher from the KMS key; stub the
    # key source so the wiring test does not require BSVIBE_GATEWAY_KMS_KEY_B64.
    # merge_watch_runtime imports the name directly, so patch it there.
    monkeypatch.setattr(
        "backend.workflow.application.runtime.merge_watch_runtime._key_from_settings",
        lambda: b"0" * 32,
    )
    settings = get_settings().model_copy(update={"github_auto_merge_enabled": True})
    names = _names(settings)
    assert "merge_watch_worker" in names
