"""ChatGPT (GPT) conversation export connector — knowledge import (Lift Q3-GPT).

Imports the ZIP export produced by OpenAI → Settings → Data Controls →
Export Data. The bundle's ``conversations.json`` carries a *graph*
(``mapping`` with parent/child node references) — distinct from Claude's
flat ``chat_messages`` array. The parser linearises the canonical
branch (first-child path) and the renderer emits the same markdown
shape used by the Claude / Notion / Obsidian connectors so
``IngestCompiler`` can dedup + classify uniformly.
"""

__all__: list[str] = []
