"""canon-watcher — filesystem watcher for canon-rooted vault edits.

Per Handoff §15.3: ``self-host only``. Detects external edits (Obsidian,
git checkout, manual file ops) under ``concepts/`` / ``proposals/`` /
``actions/`` / ``decisions/`` and emits ``NOTE_UPDATED`` /
``NOTE_DELETED`` events. ``CanonicalizationIndexSubscriber`` (slice 5)
already listens to those and calls ``index.invalidate(path)``.

Implementation note (slice 6 deviation): the spec describes this as a
plugin in ``plugins/canon-watcher/``. A long-lived ``watchdog.Observer``
is fundamentally a daemon, not a one-shot ``execute(context)`` plugin.
We ship the watcher as a core module gated by
``runtime_config.canon_watcher_enabled``, started in ``AppState.initialize``.
The ``plugins/canon-watcher/plugin.py`` shim is a no-op marker for
discoverability — operators flip the runtime flag, not the plugin file.

SaaS deployments where every write flows through the API can leave
``canon_watcher_enabled = False``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from backend.knowledge._internal.events import EventType, emit_event
from backend.knowledge._internal.tasks import spawn_task

if TYPE_CHECKING:
    from backend.knowledge._internal.events import EventBus

logger = structlog.get_logger(__name__)


_CANON_PATH_PREFIXES: tuple[str, ...] = (
    "concepts/",
    "proposals/",
    "actions/",
    "decisions/",
)


def is_canon_path(rel_path: str) -> bool:
    """True iff ``rel_path`` is under a canon root."""
    return rel_path.endswith(".md") and rel_path.startswith(_CANON_PATH_PREFIXES)


class CanonWatcher:
    """Watch ``vault_root`` for canon-rooted file changes and emit events.

    Lifecycle:
      ``start()``  — spawn a watchdog Observer (background thread) that
                     bridges fs events into asyncio EventBus emissions.
      ``stop()``   — gracefully stop the observer + drain pending tasks.
    """

    def __init__(self, vault_root: Path, event_bus: EventBus) -> None:
        self._vault_root = vault_root.resolve()
        self._event_bus = event_bus
        self._observer: Any = None
        self._loop: Any = None

    def start(self) -> None:
        """Begin watching. No-op if already started OR if watchdog is
        unavailable (logged as warning, not raised — operators get an
        empty-state lint report instead of a crash on import)."""
        if self._observer is not None:
            return
        try:
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "canon_watcher_disabled_no_watchdog",
                vault_root=str(self._vault_root),
            )
            return

        import asyncio

        self._loop = asyncio.get_running_loop()
        handler = _CanonFsEventHandler(self)
        observer = Observer()
        observer.schedule(handler, str(self._vault_root), recursive=True)
        observer.start()
        self._observer = observer
        logger.info("canon_watcher_started", vault_root=str(self._vault_root))

    def stop(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=2.0)
        except Exception as exc:  # noqa: BLE001 — log and continue, never block shutdown
            logger.warning("canon_watcher_stop_failed", error=str(exc))
        self._observer = None

    # Internal — called from watchdog's worker thread, must marshal back
    # to the asyncio loop.
    def _enqueue_emit(self, event_type: EventType, payload: dict[str, Any]) -> None:
        if self._loop is None:
            return

        async def _go() -> None:
            await emit_event(self._event_bus, event_type.name, payload, correlation_id="")

        # call_soon_threadsafe schedules the coroutine on the asyncio
        # loop without blocking the watchdog worker thread.
        self._loop.call_soon_threadsafe(lambda: spawn_task(_go(), name="canon_watcher.emit"))

    # ------------------------------------------------------ filtering

    def relativize(self, fs_path: str) -> str | None:
        """Convert an absolute fs path to vault-relative POSIX, or None
        if it falls outside the vault root."""
        try:
            rel = Path(fs_path).resolve().relative_to(self._vault_root)
        except ValueError:
            return None
        return rel.as_posix()


class _CanonFsEventHandler:
    """Watchdog FileSystemEventHandler bridge — defined lazily so
    importing this module without watchdog installed doesn't crash."""

    def __init__(self, watcher: CanonWatcher) -> None:
        self._watcher = watcher

    def dispatch(self, event: Any) -> None:
        # Watchdog calls dispatch() for every event type; we only care
        # about modify/create/delete on .md files under canon roots.
        if getattr(event, "is_directory", False):
            return
        kind = getattr(event, "event_type", "")
        src = getattr(event, "src_path", "")
        if not src:
            return
        rel = self._watcher.relativize(src)
        if rel is None or not is_canon_path(rel):
            return
        # Emit absolute path so existing event consumers
        # (GraphSubscriber / IndexSubscriber / VectorSubscriber) — which
        # are tuned to GardenWriter's ``str(resolved)`` convention — can
        # process the event uniformly. The canon subscriber normalizes
        # both forms via :func:`is_canon_path` substring detection.
        abs_src = str(Path(src).resolve())
        if kind == "deleted":
            self._watcher._enqueue_emit(  # noqa: SLF001 — sister module
                EventType.NOTE_DELETED, {"path": abs_src}
            )
        elif kind in {"created", "modified", "moved"}:
            # ``moved`` also has dest_path; emit for the dest
            if kind == "moved":
                dest = getattr(event, "dest_path", "")
                rel_dest = self._watcher.relativize(dest) if dest else None
                if rel_dest and is_canon_path(rel_dest):
                    self._watcher._enqueue_emit(  # noqa: SLF001
                        EventType.NOTE_UPDATED, {"path": str(Path(dest).resolve())}
                    )
            else:
                self._watcher._enqueue_emit(  # noqa: SLF001
                    EventType.NOTE_UPDATED, {"path": abs_src}
                )

    # Watchdog also dispatches via these handler hooks on some Observer
    # impls; alias them to ``dispatch`` so we don't miss events.
    on_created = dispatch
    on_modified = dispatch
    on_deleted = dispatch
    on_moved = dispatch
