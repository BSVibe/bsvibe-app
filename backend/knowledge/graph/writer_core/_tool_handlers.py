"""_WriterToolHandlersMixin — LLM tool-call adapters.

Translates tool args (dict from LLM tool calls) into the underlying
writer mutation/IO calls. Extracted from the original monolithic
``writer_core.py`` during Lift L1 (v8 §17.3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.knowledge.graph.note import GardenNote
from backend.knowledge.graph.vault import Vault


class _WriterToolHandlersMixin:
    """LLM tool-call adapters — translate tool args to writer methods."""

    _vault: Vault

    async def write_garden(  # pragma: no cover - in IO mixin
        self, note: GardenNote | dict[str, Any]
    ) -> Path:
        raise NotImplementedError

    async def write_seed(  # pragma: no cover - in IO mixin
        self, source: str, data: dict[str, Any]
    ) -> Path:
        raise NotImplementedError

    async def update_note(  # pragma: no cover - in mutation mixin
        self, path: str, content: str, *, preserve_frontmatter: bool = True
    ) -> Path:
        raise NotImplementedError

    async def append_to_note(  # pragma: no cover - in mutation mixin
        self, path: str, text: str
    ) -> Path:
        raise NotImplementedError

    async def delete_note(self, path: str) -> None:  # pragma: no cover - in mutation mixin
        ...

    async def handle_write_note(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handle a write-note tool call from the LLM."""
        title = args.get("title", "Untitled")
        content = args.get("content", "")
        tags = args.get("tags", [])
        entities = args.get("entities", [])

        path = await self.write_garden(
            {
                "title": title,
                "content": content,
                "source": "chat",
                "tags": tags,
                "entities": entities,
            }
        )
        return {"status": "saved", "title": title, "path": str(path)}

    async def handle_write_seed(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handle a write-seed tool call from the LLM.

        Source is always ``"idea"`` to separate user ideas from automatic
        data captures.
        """
        title = args.get("title", "Untitled")
        content = args.get("content", "")
        tags = args.get("tags", [])
        data: dict[str, Any] = {"title": title, "content": content}
        if tags:
            data["tags"] = tags
        path = await self.write_seed("idea", data)
        return {"status": "saved", "title": title, "path": str(path)}

    async def handle_update_note(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handle an update-note tool call from the LLM."""
        path = args["path"]
        content = args["content"]
        preserve = args.get("preserve_frontmatter", True)
        resolved = await self.update_note(path, content, preserve_frontmatter=preserve)
        return {"status": "updated", "path": str(resolved)}

    async def handle_append_note(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handle an append-note tool call from the LLM."""
        path = args["path"]
        text = args["text"]
        await self.append_to_note(path, text)
        resolved = self._vault.resolve_path(path)
        return {"status": "appended", "path": str(resolved)}

    async def handle_delete_note(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handle a delete-note tool call from the LLM."""
        path = args["path"]
        await self.delete_note(path)
        return {"status": "deleted", "path": path}
