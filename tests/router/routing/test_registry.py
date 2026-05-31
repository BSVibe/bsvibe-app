"""ModelRegistryService — yaml-union-DB merge + TTL cache + invalidate."""

from __future__ import annotations

import uuid

from backend.router.routing.registry import (
    DbModelRow,
    ModelRegistryService,
)

WS = uuid.uuid4()
ACCT = uuid.uuid4()


class _FakeRepo:
    def __init__(self, rows: list[DbModelRow]) -> None:
        self.rows = rows
        self.calls = 0

    async def list_for_account(
        self, *, workspace_id: uuid.UUID, account_id: uuid.UUID
    ) -> list[DbModelRow]:
        self.calls += 1
        return [
            r for r in self.rows if r.workspace_id == workspace_id and r.account_id == account_id
        ]


def _custom(name: str) -> DbModelRow:
    return DbModelRow(
        id=uuid.uuid4(),
        workspace_id=WS,
        account_id=ACCT,
        name=name,
        origin="custom",
        litellm_model=f"litellm-{name}",
        litellm_params=None,
        is_passthrough=True,
    )


def _hide(name: str) -> DbModelRow:
    return DbModelRow(
        id=uuid.uuid4(),
        workspace_id=WS,
        account_id=ACCT,
        name=name,
        origin="hide_system",
        litellm_model=None,
        litellm_params=None,
        is_passthrough=False,
    )


class TestMerge:
    async def test_yaml_only(self):
        repo = _FakeRepo([])
        svc = ModelRegistryService(
            yaml_models=[{"name": "gpt-4o", "litellm_model": "gpt-4o"}],
            repo=repo,
        )
        models = await svc.list_models(workspace_id=WS, account_id=ACCT)
        assert [m.name for m in models] == ["gpt-4o"]
        assert models[0].origin == "system"

    async def test_custom_replaces_yaml(self):
        repo = _FakeRepo([_custom("gpt-4o")])
        svc = ModelRegistryService(
            yaml_models=[{"name": "gpt-4o", "litellm_model": "gpt-4o"}],
            repo=repo,
        )
        models = await svc.list_models(workspace_id=WS, account_id=ACCT)
        # One entry — the custom override.
        assert len(models) == 1
        assert models[0].origin == "custom"

    async def test_hide_system_subtracts_yaml(self):
        repo = _FakeRepo([_hide("hidden-model")])
        svc = ModelRegistryService(
            yaml_models=[
                {"name": "hidden-model", "litellm_model": "x"},
                {"name": "visible-model", "litellm_model": "y"},
            ],
            repo=repo,
        )
        models = await svc.list_models(workspace_id=WS, account_id=ACCT)
        assert [m.name for m in models] == ["visible-model"]

    async def test_custom_beats_hide_for_same_name(self):
        # If both rows target the same name, the custom replacement wins
        # (replaces the yaml entry; hide is moot).
        repo = _FakeRepo([_custom("dual"), _hide("dual")])
        svc = ModelRegistryService(
            yaml_models=[{"name": "dual", "litellm_model": "y"}],
            repo=repo,
        )
        models = await svc.list_models(workspace_id=WS, account_id=ACCT)
        assert len(models) == 1
        assert models[0].origin == "custom"

    async def test_unknown_origin_ignored(self):
        bad = DbModelRow(
            id=uuid.uuid4(),
            workspace_id=WS,
            account_id=ACCT,
            name="bad",
            origin="not_a_real_origin",
            litellm_model=None,
            litellm_params=None,
            is_passthrough=False,
        )
        repo = _FakeRepo([bad])
        svc = ModelRegistryService(yaml_models=[], repo=repo)
        models = await svc.list_models(workspace_id=WS, account_id=ACCT)
        assert models == []


class TestCache:
    async def test_hits_repo_once_within_ttl(self):
        repo = _FakeRepo([_custom("m")])
        clock = [0.0]
        svc = ModelRegistryService(
            yaml_models=[],
            repo=repo,
            cache_ttl_s=30,
            clock=lambda: clock[0],
        )
        await svc.list_models(workspace_id=WS, account_id=ACCT)
        await svc.list_models(workspace_id=WS, account_id=ACCT)
        assert repo.calls == 1

    async def test_refetches_after_ttl(self):
        repo = _FakeRepo([_custom("m")])
        clock = [0.0]
        svc = ModelRegistryService(
            yaml_models=[],
            repo=repo,
            cache_ttl_s=30,
            clock=lambda: clock[0],
        )
        await svc.list_models(workspace_id=WS, account_id=ACCT)
        clock[0] = 60.0
        await svc.list_models(workspace_id=WS, account_id=ACCT)
        assert repo.calls == 2

    async def test_invalidate_evicts(self):
        repo = _FakeRepo([_custom("m")])
        svc = ModelRegistryService(yaml_models=[], repo=repo)
        await svc.list_models(workspace_id=WS, account_id=ACCT)
        await svc.invalidate(workspace_id=WS, account_id=ACCT)
        await svc.list_models(workspace_id=WS, account_id=ACCT)
        assert repo.calls == 2

    async def test_separate_accounts_cache_independently(self):
        a = uuid.uuid4()
        b = uuid.uuid4()
        rows = [
            DbModelRow(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=acct,
                name=f"m-{acct.hex[:4]}",
                origin="custom",
                litellm_model="x",
                litellm_params=None,
                is_passthrough=True,
            )
            for acct in (a, b)
        ]
        repo = _FakeRepo(rows)
        svc = ModelRegistryService(yaml_models=[], repo=repo)
        a_models = await svc.list_models(workspace_id=WS, account_id=a)
        b_models = await svc.list_models(workspace_id=WS, account_id=b)
        assert a_models[0].name != b_models[0].name


class TestPassthroughSet:
    async def test_passthrough_set_filters_non_passthrough(self):
        repo = _FakeRepo(
            [
                DbModelRow(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    name="pass",
                    origin="custom",
                    litellm_model="x",
                    litellm_params=None,
                    is_passthrough=True,
                ),
                DbModelRow(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    name="no-pass",
                    origin="custom",
                    litellm_model="y",
                    litellm_params=None,
                    is_passthrough=False,
                ),
            ]
        )
        svc = ModelRegistryService(yaml_models=[], repo=repo)
        passable = await svc.get_passthrough_set(workspace_id=WS, account_id=ACCT)
        assert passable == {"pass"}
