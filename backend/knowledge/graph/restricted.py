"""Read + seed-only wrapper around :class:`GardenWriter`.

External surfaces (plugins, MCP) get this wrapper instead of the raw writer
so they cannot edit garden notes directly. They can only:

* write seeds (raw collected data)
* read existing notes (for context)
* resolve a per-plugin state path (cursor/offset persistence)

Anything that would mutate ``garden/`` (``write_garden``, ``update_note``,
``append_to_note``, ``delete_note``, ``mark_*``) goes through
``IngestCompiler`` instead, which is the single sanctioned write surface.

Lifted (new wrapper, no behavior change) from ``bsage.core.skill_context``;
re-homed under :mod:`backend.knowledge.graph` because the wrapper guards the
knowledge module's write boundary — its proper owner is the graph package,
not the plugin runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.knowledge.graph.writer import GardenWriter


class RestrictedPluginGarden:
    """Restricted GardenWriter view for plugins / MCP / external callers."""

    __slots__ = ("_writer",)

    _BLOCKED: tuple[str, ...] = (
        "write_garden",
        "update_note",
        "append_to_note",
        "delete_note",
        "mark_evergreen",
        "mark_archived",
        "promote_status",
        "handle_write_note",
        "handle_update_note",
        "handle_append_note",
        "handle_delete_note",
    )

    def __init__(self, writer: GardenWriter) -> None:
        self._writer = writer

    async def write_seed(self, source: str, data: dict[str, Any]) -> Path:
        return await self._writer.write_seed(source, data)

    async def write_input_log(self, source: str, raw_summary: str) -> None:
        await self._writer.write_input_log(source, raw_summary)

    async def write_action(self, name: str, summary: str) -> None:
        await self._writer.write_action(name, summary)

    async def read_notes(self, subdir: str) -> list[Path]:
        return await self._writer.read_notes(subdir)

    async def read_note_content(self, path: Path) -> str:
        return await self._writer.read_note_content(path)

    def resolve_plugin_state_path(self, plugin_name: str, subpath: str = "_state.json") -> Path:
        return self._writer.resolve_plugin_state_path(plugin_name, subpath)

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED:
            raise PermissionError(
                f"'{name}' is not available to plugins/MCP — submit a seed "
                "via write_seed() and let IngestCompiler classify it."
            )
        raise AttributeError(name)
