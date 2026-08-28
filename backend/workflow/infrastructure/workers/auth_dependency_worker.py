"""AuthDependencyWorker — tell the founder when nobody can sign in.

Prod 2026-08-28: the Supabase project backing ``USER_JWT_JWKS_URL`` was paused
(free-tier idle), which removes its subdomain from DNS entirely. Sign-in
returned 500 and every ``get_current_user`` route 401'd with "JWKS resolution
failed" — and nothing noticed. The host uptime probe calls the deployment
healthy on ANY HTTP response, and the founder had been working through MCP,
whose tokens are verified against LOCAL keys and never touch that host.

The failure was invisible because the only passive signal is a real sign-in
failing, and during a quiet stretch there are none. So this worker probes
actively and emits through the same outbox every other notification uses.

It only PRODUCES; the :class:`NotifyWorker` fans the row out. For this event
that worker additionally defaults its push channels ON for anything bound
(``DEFAULT_ON_EVENTS``) — the in-app inbox is part of what breaks here, so an
alert waiting to be opted into is an alert nobody gets.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.copy import (
    AUTH_DOWN_LINK,
    auth_recovered_copy,
    notification_copy,
)
from backend.notifications.emit import emit_notification
from backend.shared.authz.probe import UserKeySourceStatus, check_user_key_source
from backend.shared.authz.settings import get_settings as get_authz_settings
from backend.workers.base import BaseWorker

logger = structlog.get_logger(__name__)

#: Must be listed in ``NOTIFICATION_OUTBOX.producers`` (the INV-1 producer guard).
PRODUCER_ID = "worker:auth_dependency"

#: Five minutes. The outage lasts as long as the dependency does, so probing
#: harder buys nothing — and each probe is a real network call.
_POLL_INTERVAL_S = 300.0

#: Key sources whose failure is an outage THIS process actually watched happen.
#:
#: A reading of ``unconfigured`` means the settings in this process hold no key
#: material — which says nothing about whether anyone can sign in, only that
#: this process cannot tell. Paging on it is claiming an observation that was
#: never made. Shipped on 2026-08-28 without this rule and it fired within
#: seconds of deploy: the worker container had not been given ``USER_JWT_*``, so
#: the settings fell back to HS256-with-no-secret and three false "Sign-in is
#: down" alerts went out, two of them to telegram, while sign-in was fine.
#:
#: This rule alone would leave the detector permanently silent in that same
#: broken deployment, so the compose passthrough is guarded separately
#: (``tests/deploy/test_auth_probe_env_reaches_the_worker.py``). A detector that
#: cannot see is worse than none — it looks present.
_OBSERVABLE_SOURCES: frozenset[str] = frozenset({"jwks_url"})


def _today() -> str:
    return datetime.now(tz=UTC).date().isoformat()


class AuthDependencyWorker(BaseWorker):
    """Probe the user-JWT key source; notify on the transitions."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        probe: Callable[[], Awaitable[UserKeySourceStatus]] | None = None,
        today: Callable[[], str] = _today,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        super().__init__(name="auth_dependency_worker", poll_interval_s=poll_interval_s)
        self._sf = session_factory
        self._probe = probe or self._default_probe
        self._today = today
        # In-memory: a restart forgets an outage it never announced, which is
        # what we want — a fresh process must not announce a recovery from an
        # incident it did not observe.
        self._announced_down = False

    @staticmethod
    async def _default_probe() -> UserKeySourceStatus:
        return await check_user_key_source(get_authz_settings())

    async def _tick(self) -> int:
        return await self.check_once()

    async def check_once(self) -> int:
        """One probe + the notification its transition implies.

        Returns how many workspaces were staged an alert this tick — 0 on the
        quiet path and on a repeat while still down (the dedupe key makes the
        repeat a DB-level no-op, so the count reflects rows staged, not rows
        that survived).
        """
        status = await self._probe()
        if not status.ok and status.source not in _OBSERVABLE_SOURCES:
            # Nothing observed — stay silent AND leave the down/up state alone,
            # so this reading cannot manufacture an all-clear later either.
            logger.info(
                "auth_dependency_not_observable",
                source=status.source,
                detail=status.detail,
            )
            return 0

        if not status.ok:
            if not self._announced_down:
                logger.warning("auth_dependency_down", source=status.source, detail=status.detail)
            staged = await self._announce(status, recovered=False)
            self._announced_down = True
            return staged

        if self._announced_down:
            logger.info("auth_dependency_recovered", source=status.source)
            self._announced_down = False
            return await self._announce(status, recovered=True)
        return 0

    async def _announce(self, status: UserKeySourceStatus, *, recovered: bool) -> int:
        state = "up" if recovered else "down"
        async with self._sf() as session:
            workspaces = (await session.execute(select(WorkspaceRow))).scalars().all()
            for ws in workspaces:
                if recovered:
                    copy = auth_recovered_copy(ws.language)
                    body = copy.body
                else:
                    copy = notification_copy("auth_down", ws.language)
                    body = f"{copy.body} ({status.detail})" if status.detail else copy.body
                await emit_notification(
                    session,
                    workspace_id=ws.id,
                    event="auth_down",
                    # Date-bucketed: one alert per workspace per day while the
                    # outage lasts. The UNIQUE dedupe_key makes the repeat a
                    # DB-level no-op rather than a decision this worker has to
                    # remember across restarts.
                    dedupe_key=f"auth_down:{ws.id}:{state}:{self._today()}",
                    payload={
                        "title": copy.title,
                        "body": body,
                        "link": AUTH_DOWN_LINK,
                        "recovered": recovered,
                        "source": status.source,
                    },
                    producer_id=PRODUCER_ID,
                )
            await session.commit()
        return len(workspaces)


__all__ = ["PRODUCER_ID", "AuthDependencyWorker"]
