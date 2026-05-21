"""LLM tool-call definitions used by ``GardenWriter`` and ``AgentLoop``.

Split out of ``bsage/garden/writer.py`` (M15, Hardening Sprint 2). The
constants are pure data (``dict``s in OpenAI tool-call shape) and have no
runtime dependencies — keeping them in their own module lets callers import
the schemas without paying the cost of loading the rest of ``writer``.
"""

from __future__ import annotations

from typing import Any

WRITE_NOTE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write-note",
        "description": (
            "Write a processed garden note. Use [[wikilinks]] in content "
            "for any concept, person, tool, project, or organization — the "
            "system auto-creates entity stubs. Tags describe what the note "
            "is ABOUT (domain, topic), not what KIND it is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the note"},
                "content": {
                    "type": "string",
                    "description": "Markdown body. Use [[wikilinks]] liberally.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                    "description": (
                        "2-5 free-form lowercase content tags "
                        '(e.g. "self-hosting", "reverse-proxy"). Avoid kind '
                        'tags like "idea" / "fact" / "insight".'
                    ),
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string", "pattern": r"^\[\[.+\]\]$"},
                    "description": (
                        "[[Name]] strings extracted from content. Each MUST "
                        "appear as a wikilink in content."
                    ),
                },
            },
            "required": ["title", "content"],
        },
    },
}

WRITE_SEED_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write-seed",
        "description": (
            "Save a seed note — raw ideas, fleeting thoughts, or "
            "unprocessed data. Use by default when the user wants "
            "to save something new."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short title for the seed",
                },
                "content": {
                    "type": "string",
                    "description": "Body text of the idea or data",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization",
                },
            },
            "required": ["title", "content"],
        },
    },
}


UPDATE_NOTE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update-note",
        "description": (
            "Update the content of an existing vault note. "
            "Use when modifying, replacing, or adding links to an existing note."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path (e.g. garden/seedling/my-note.md)",
                },
                "content": {
                    "type": "string",
                    "description": "New markdown body content",
                },
                "preserve_frontmatter": {
                    "type": "boolean",
                    "description": "Keep existing YAML frontmatter (default: true)",
                },
            },
            "required": ["path", "content"],
        },
    },
}

DELETE_NOTE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "delete-note",
        "description": (
            "Delete a note from the vault. Cannot delete action logs. "
            "Use when a note is outdated, duplicated, or no longer needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path to delete",
                },
            },
            "required": ["path"],
        },
    },
}

APPEND_NOTE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "append-note",
        "description": (
            "Append text to an existing vault note. "
            "Use when adding new content without replacing what already exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path (e.g. garden/seedling/my-note.md)",
                },
                "text": {
                    "type": "string",
                    "description": "Text to append to the note",
                },
            },
            "required": ["path", "text"],
        },
    },
}

SEARCH_VAULT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search-vault",
        "description": (
            "Search the vault for relevant notes using semantic search. "
            "Use to find related notes, check for duplicates, or gather context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "context_dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Vault subdirectories to search "
                        "(default: seeds, garden/seedling, garden/budding, garden/evergreen)"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max notes to return (default: 10)",
                },
            },
            "required": ["query"],
        },
    },
}


__all__ = [
    "APPEND_NOTE_TOOL",
    "DELETE_NOTE_TOOL",
    "SEARCH_VAULT_TOOL",
    "UPDATE_NOTE_TOOL",
    "WRITE_NOTE_TOOL",
    "WRITE_SEED_TOOL",
]
