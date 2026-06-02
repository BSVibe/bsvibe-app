"""Notion block tree → markdown converter.

Standalone (no external deps); maps the 8 MVP block types from the spec
plus ``divider``, ``child_page``, and ``image`` to GitHub-flavoured
markdown. Unknown block types degrade to a ``<!-- unsupported: <type> -->``
placeholder so a malformed block never breaks an import batch.

The converter is intentionally pure: in ``render_blocks`` we walk an
in-memory block tree (each block may carry a ``children`` list for nested
content). The plugin orchestrator is responsible for paginated
``/blocks/{id}/children`` fetches *before* calling here — keeping I/O
separate from rendering lets the converter stay test-trivial.

Title extraction (:func:`extract_page_title`) tolerates both the canonical
``properties.title.title[0].plain_text`` shape *and* the database-page
convention where the title property is renamed (``Name`` by default in
Notion templates). We scan for any property whose ``type == "title"``.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ── rich text ──────────────────────────────────────────────────────────────


def _apply_annotations(plain: str, annotations: dict[str, Any]) -> str:
    """Wrap ``plain`` with markdown markers for the active annotations.

    Order matters: code wraps innermost (no further interpretation
    inside), strikethrough next, then italic, then bold. ``bold+italic``
    → ``***x***``.
    """
    if not plain:
        return ""
    # Inline code is the strongest claim — anything inside is verbatim.
    if annotations.get("code"):
        return f"`{plain}`"
    out = plain
    if annotations.get("strikethrough"):
        out = f"~~{out}~~"
    bold = annotations.get("bold")
    italic = annotations.get("italic")
    if bold and italic:
        out = f"***{out}***"
    elif bold:
        out = f"**{out}**"
    elif italic:
        out = f"*{out}*"
    return out


def rich_text_to_markdown(rich_text: list[dict[str, Any]] | None) -> str:
    """Render a Notion ``rich_text`` array to inline markdown.

    Empty / ``None`` input → empty string. Each segment's annotations are
    rendered independently then concatenated (so ``a**b**c`` is two
    plain segments around one bold segment).
    """
    if not rich_text:
        return ""
    parts: list[str] = []
    for segment in rich_text:
        if not isinstance(segment, dict):
            continue
        # ``plain_text`` is always populated by the Notion API; fall back
        # to ``text.content`` for forward-compat with future segment kinds.
        plain = segment.get("plain_text")
        if plain is None:
            text_field = segment.get("text") or {}
            plain = text_field.get("content", "")
        annotations = segment.get("annotations") or {}
        rendered = _apply_annotations(plain, annotations)
        # Link rendering wraps the *already-annotated* run so a bold link
        # is ``[**text**](url)``.
        href = segment.get("href")
        if href:
            rendered = f"[{rendered}]({href})"
        parts.append(rendered)
    return "".join(parts)


# ── block rendering ────────────────────────────────────────────────────────

# 8 MVP block types from the spec + divider/child_page/image. Anything
# else falls through to the placeholder branch in :func:`_render_block`.
_SUPPORTED_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "code",
        "quote",
        "to_do",
        "divider",
        "child_page",
        "image",
    }
)


def _block_rich_text(block: dict[str, Any], block_type: str) -> str:
    payload = block.get(block_type) or {}
    return rich_text_to_markdown(payload.get("rich_text", []))


def _render_image(block: dict[str, Any]) -> str:
    payload = block.get("image") or {}
    # Notion exposes either ``external.url`` or ``file.url`` depending on
    # how the image was uploaded.
    kind = payload.get("type")
    url = ""
    if kind == "external":
        url = (payload.get("external") or {}).get("url", "")
    elif kind == "file":
        url = (payload.get("file") or {}).get("url", "")
    else:
        # Some responses skip the discriminator and inline the url.
        url = (payload.get("external") or payload.get("file") or {}).get("url", "")
    caption = rich_text_to_markdown(payload.get("caption", []))
    alt = caption or "image"
    if not url:
        return "<!-- image: missing url -->"
    return f"![{alt}]({url})"


def _render_to_do(block: dict[str, Any], indent: str) -> list[str]:
    payload = block.get("to_do") or {}
    checked = bool(payload.get("checked"))
    text = rich_text_to_markdown(payload.get("rich_text", []))
    mark = "x" if checked else " "
    return [f"{indent}- [{mark}] {text}"]


def _render_code(block: dict[str, Any], indent: str) -> list[str]:
    payload = block.get("code") or {}
    language = payload.get("language") or ""
    body = rich_text_to_markdown(payload.get("rich_text", []))
    lines = [f"{indent}```{language}"]
    for code_line in body.splitlines() or [body]:
        lines.append(f"{indent}{code_line}")
    lines.append(f"{indent}```")
    return lines


def _render_child_page(block: dict[str, Any], indent: str) -> list[str]:
    # Gotcha #4: child_page is a *link* to another page, not embedded
    # content. The spec is explicit: do NOT recurse into child pages
    # (that would explode the import). Emit a marker so the founder
    # can see the link existed.
    title = (block.get("child_page") or {}).get("title", "")
    return [f"{indent}Linked: {title}".rstrip()]


# Dispatch table keeps :func:`_render_block` branch-count below the
# pylint/PLR threshold. Each handler returns a list of indented lines.
_BLOCK_RENDERERS: dict[str, Any] = {
    "paragraph": lambda b, ind: [f"{ind}{_block_rich_text(b, 'paragraph')}"],
    "heading_1": lambda b, ind: [f"{ind}# {_block_rich_text(b, 'heading_1')}"],
    "heading_2": lambda b, ind: [f"{ind}## {_block_rich_text(b, 'heading_2')}"],
    "heading_3": lambda b, ind: [f"{ind}### {_block_rich_text(b, 'heading_3')}"],
    "bulleted_list_item": lambda b, ind: [f"{ind}- {_block_rich_text(b, 'bulleted_list_item')}"],
    "numbered_list_item": lambda b, ind: [f"{ind}1. {_block_rich_text(b, 'numbered_list_item')}"],
    "quote": lambda b, ind: [f"{ind}> {_block_rich_text(b, 'quote')}"],
    "divider": lambda b, ind: [f"{ind}---"],
    "to_do": _render_to_do,
    "code": _render_code,
    "child_page": _render_child_page,
    "image": lambda b, ind: [f"{ind}{_render_image(b)}"],
}


def _render_block(block: dict[str, Any], depth: int = 0) -> list[str]:
    """Render one block (+ its children, recursively) to a list of lines."""
    block_type = block.get("type") or ""
    indent = "  " * depth
    renderer = _BLOCK_RENDERERS.get(block_type)
    if renderer is None:
        logger.debug(
            "notion_unsupported_block",
            block_type=block_type,
            block_id=block.get("id"),
        )
        lines = [f"{indent}<!-- unsupported: {block_type} -->"]
    else:
        lines = list(renderer(block, indent))

    # Walk in-memory children. The plugin orchestrator pre-fetches via
    # ``/blocks/{id}/children`` and attaches them as ``block['children']``
    # so this stays pure. ``child_page`` short-circuits (it's a link, not
    # embedded content).
    if block.get("has_children") and block_type != "child_page":
        for child in block.get("children", []) or []:
            lines.extend(_render_block(child, depth=depth + 1))

    return lines


def render_blocks(blocks: list[dict[str, Any]]) -> str:
    """Render a flat (or nested) list of Notion blocks to markdown.

    Adjacent block outputs are separated by a single blank line to keep
    paragraphs and headings visually distinct in the produced markdown
    (downstream tooling — IngestCompiler — is tolerant of either
    one or two blanks).
    """
    sections: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        rendered = _render_block(block)
        sections.append("\n".join(rendered))
    # Join with double newlines so each top-level block becomes a
    # distinct markdown paragraph.
    return "\n\n".join(s for s in sections if s)


# ── page metadata ──────────────────────────────────────────────────────────


def extract_page_title(page: dict[str, Any]) -> str:
    """Pull the title plain-text from a Notion page object.

    Notion stores titles inside ``properties.<title-prop>.title[]`` — the
    property name varies (canonical ``title`` for top-level pages,
    ``Name`` by convention on database pages). We scan all properties for
    one whose ``type == "title"`` and concatenate every ``plain_text``.
    Returns the empty string when no title can be located rather than
    raising, so a malformed page never breaks the import.
    """
    if not isinstance(page, dict):
        return ""
    props = page.get("properties") or {}
    if not isinstance(props, dict):
        return ""
    for prop in props.values():
        if not isinstance(prop, dict):
            continue
        if prop.get("type") != "title":
            continue
        title_segments = prop.get("title")
        if not title_segments:
            continue
        out = "".join(seg.get("plain_text", "") for seg in title_segments if isinstance(seg, dict))
        if out:
            return out
    return ""


__all__ = [
    "extract_page_title",
    "render_blocks",
    "rich_text_to_markdown",
]
