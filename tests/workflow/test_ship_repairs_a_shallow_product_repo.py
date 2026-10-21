"""#929 — a ship can publish a private product that was cloned shallow.

The ship path holds the run's workspace session, so it is where the product's github credential
can be resolved (a refresh writes the rotated token back in that same transaction). It hands
publish a credential CALLBACK: nothing is resolved unless the repo is actually shallow. The idle
reaper reads across tenants and may not write, so it keeps refusing — the next ship repairs.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest

from backend.workflow.application import product_repo_credential as module
from backend.workflow.application.product_repo_credential import product_repo_credential


async def test_the_credential_is_the_workspaces_github_token(monkeypatch) -> None:
    seen: list[uuid.UUID] = []

    async def _resolve(session: Any, *, workspace_id: uuid.UUID, cipher: Any) -> str | None:
        seen.append(workspace_id)
        return "ghs_x"

    monkeypatch.setattr(module, "_resolve_github_token", _resolve)
    monkeypatch.setattr(module, "_cipher", lambda: object())
    ws = uuid.uuid4()

    credential = product_repo_credential(object(), ws)

    assert seen == [], "nothing is resolved until publish asks"
    assert await credential() == "ghs_x" and seen == [ws]


async def test_a_failed_lookup_is_no_credential_not_a_failed_ship(monkeypatch) -> None:
    async def _boom(session: Any, *, workspace_id: uuid.UUID, cipher: Any) -> str | None:
        raise RuntimeError("needs_reauth")

    monkeypatch.setattr(module, "_resolve_github_token", _boom)
    monkeypatch.setattr(module, "_cipher", lambda: object())

    assert await product_repo_credential(object(), uuid.uuid4())() is None


@pytest.mark.parametrize(
    ("module_path", "function"),
    [
        ("backend.workflow.application.agent_runner", "AgentRunner._push_bundle_best_effort"),
        ("backend.workflow.application.checkpoint_resolution", None),
    ],
)
def test_every_ship_publish_offers_the_credential(module_path: str, function: str | None) -> None:
    import importlib

    mod = importlib.import_module(module_path)
    obj: Any = mod
    for part in (function or "").split("."):
        if part:
            obj = getattr(obj, part)
    src = "".join(inspect.getsource(obj).split())
    calls = src.count("awaitpublish_product_bundle(")
    assert calls >= 1, module_path
    assert src.count("credential=product_repo_credential(") == calls, module_path
