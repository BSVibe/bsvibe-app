"""Workspace OUTPUT language — the language LLM-generated, user-facing prose is
written in (knowledge notes, the agent's decision questions, framing).

The founder sets ``workspaces.language`` via Settings → Language. A run / ingest
entry point loads the workspace and calls :func:`set_output_language` once; every
downstream generation prompt then appends :func:`language_directive` to its
system message so the model writes prose in that language. Threaded via a
``ContextVar`` so the four prompt sites don't each need to plumb the workspace
through their call chain.

Scope: prose ONLY. Code, identifiers, file paths, shell commands stay verbatim —
the directive says so explicitly. ``en`` (the default) adds nothing, so English
workspaces pay zero prompt overhead.
"""

from __future__ import annotations

from contextvars import ContextVar

_DEFAULT = "en"

# Human-readable name per supported locale tag. Unknown tags fall back to the
# tag itself in the directive (still a usable instruction).
_LANGUAGE_NAME: dict[str, str] = {
    "en": "English",
    "ko": "Korean (한국어)",
}

_output_language: ContextVar[str] = ContextVar("bsvibe_output_language", default=_DEFAULT)


def set_output_language(language: str | None) -> None:
    """Set the current generation output language (run / ingest entry point)."""
    _output_language.set((language or _DEFAULT).strip() or _DEFAULT)


def current_output_language() -> str:
    """The output language for the current context (``en`` when unset)."""
    return _output_language.get()


def language_directive(language: str | None = None) -> str:
    """The system-prompt suffix that makes the model write prose in the workspace
    language. Empty for English (the default) so it adds nothing to the prompt.

    Pass ``language`` to override the contextvar (e.g. when the site already has
    the workspace language in hand); otherwise the contextvar is used.
    """
    lang = (language or current_output_language() or _DEFAULT).strip() or _DEFAULT
    if lang == _DEFAULT:
        return ""
    name = _LANGUAGE_NAME.get(lang, lang)
    return (
        f"\n\nWrite all user-facing prose (titles, summaries, questions, note "
        f"bodies, explanations) in {name}. Keep code, identifiers, file paths, "
        f"and shell commands unchanged."
    )
