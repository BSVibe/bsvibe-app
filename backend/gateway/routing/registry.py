"""yaml-union-DB model registry, per-account.

Merges an operator-managed yaml catalog (the "system" half) with
per-account ``model_catalog_entries`` rows. Hidden behind a per-account
TTL cache (default 60s, ``cache_ttl_s`` configurable). Mutations call
:meth:`invalidate` so changes become visible without a restart.

Merge rules:

* ``custom`` rows replace yaml entries of the same name.
* ``hide_system`` rows subtract yaml entries from the visible set.
* If a name has both ``custom`` and ``hide_system``, ``custom`` wins
  (the replacement makes the hide moot).
* Unknown ``origin`` values are ignored — defensive in case bad data
  gets in.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DbModelRow:
    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    origin: str  # 'custom' | 'hide_system'
    litellm_model: str | None
    litellm_params: dict[str, Any] | None
    is_passthrough: bool


@dataclass(frozen=True)
class ModelEntry:
    """Effective model visible to one account after merge."""

    name: str
    origin: str  # 'system' | 'custom'
    is_passthrough: bool
    litellm_model: str | None
    litellm_params: dict[str, Any] | None
    id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


class ModelCatalogReadRepo(Protocol):
    """Minimal slice the registry needs from the catalog repository."""

    async def list_for_account(
        self, *, workspace_id: uuid.UUID, account_id: uuid.UUID
    ) -> list[DbModelRow] | Any: ...


@dataclass
class _CacheEntry:
    expires_at: float
    models: tuple[ModelEntry, ...]


class ModelRegistryService:
    def __init__(
        self,
        yaml_models: list[dict[str, Any]],
        repo: ModelCatalogReadRepo,
        *,
        cache_ttl_s: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._yaml: tuple[ModelEntry, ...] = tuple(self._normalize_yaml(e) for e in yaml_models)
        self._repo = repo
        self._ttl_s = cache_ttl_s
        self._clock = clock
        self._cache: dict[tuple[uuid.UUID, uuid.UUID], _CacheEntry] = {}
        self._locks: dict[tuple[uuid.UUID, uuid.UUID], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    def _normalize_yaml(entry: dict[str, Any]) -> ModelEntry:
        return ModelEntry(
            name=entry["name"],
            origin="system",
            is_passthrough=True,
            litellm_model=entry.get("litellm_model"),
            litellm_params=entry.get("litellm_params"),
        )

    async def list_models(
        self, *, workspace_id: uuid.UUID, account_id: uuid.UUID
    ) -> list[ModelEntry]:
        key = (workspace_id, account_id)
        cached = self._cache.get(key)
        if self._fresh(cached):
            return list(cached.models)  # type: ignore[union-attr]
        lock = await self._lock_for(key)
        async with lock:
            cached = self._cache.get(key)
            if self._fresh(cached):
                return list(cached.models)  # type: ignore[union-attr]
            models = await self._compute(workspace_id=workspace_id, account_id=account_id)
            self._cache[key] = _CacheEntry(
                expires_at=self._clock() + self._ttl_s,
                models=tuple(models),
            )
            return list(models)

    async def get_passthrough_set(
        self, *, workspace_id: uuid.UUID, account_id: uuid.UUID
    ) -> set[str]:
        models = await self.list_models(workspace_id=workspace_id, account_id=account_id)
        return {m.name for m in models if m.is_passthrough}

    async def invalidate(self, *, workspace_id: uuid.UUID, account_id: uuid.UUID) -> None:
        key = (workspace_id, account_id)
        lock = await self._lock_for(key)
        async with lock:
            self._cache.pop(key, None)
        logger.info(
            "model_registry.cache_invalidate",
            workspace_id=str(workspace_id),
            account_id=str(account_id),
        )

    def _fresh(self, entry: _CacheEntry | None) -> bool:
        return entry is not None and entry.expires_at > self._clock()

    async def _lock_for(self, key: tuple[uuid.UUID, uuid.UUID]) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def _compute(self, *, workspace_id: uuid.UUID, account_id: uuid.UUID) -> list[ModelEntry]:
        rows = await self._repo.list_for_account(workspace_id=workspace_id, account_id=account_id)
        custom_by_name: dict[str, DbModelRow] = {}
        hidden_names: set[str] = set()
        for row in rows:
            if row.origin == "custom":
                custom_by_name[row.name] = row
            elif row.origin == "hide_system":
                hidden_names.add(row.name)
            # Anything else is ignored.

        merged: list[ModelEntry] = []
        for sys_entry in self._yaml:
            if sys_entry.name in custom_by_name:
                continue
            if sys_entry.name in hidden_names:
                continue
            merged.append(sys_entry)
        for row in custom_by_name.values():
            merged.append(
                ModelEntry(
                    name=row.name,
                    origin="custom",
                    is_passthrough=row.is_passthrough,
                    litellm_model=row.litellm_model,
                    litellm_params=row.litellm_params,
                    id=row.id,
                    workspace_id=row.workspace_id,
                    account_id=row.account_id,
                )
            )
        logger.info(
            "model_registry.cache_miss",
            workspace_id=str(workspace_id),
            account_id=str(account_id),
            yaml_count=len(self._yaml),
            db_row_count=len(rows),
            visible_count=len(merged),
            custom_count=len(custom_by_name),
            hidden_count=len(hidden_names),
        )
        return merged
