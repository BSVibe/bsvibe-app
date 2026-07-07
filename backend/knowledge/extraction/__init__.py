"""Worth-remembering knowledge extraction — shared by settle + ingest.

Knowledge is not a work-history log. A verified run (or an ingested file) only
deposits a note when there is something WORTH REMEMBERING — a retrospective
insight, a non-obvious learning, or a user decision/choice. Routine work leaves
nothing. This package owns the stack-agnostic core both paths reuse.
"""

from backend.knowledge.extraction.worth_remembering import (
    RememberableKnowledge,
    is_inherently_notable,
    parse_extraction,
    worth_remembering_messages,
)

__all__ = [
    "RememberableKnowledge",
    "is_inherently_notable",
    "parse_extraction",
    "worth_remembering_messages",
]
