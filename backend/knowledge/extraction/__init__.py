"""Worth-remembering knowledge — shared shape + the agent-declared parser.

Knowledge is not a work-history log. A verified run only deposits a note when
the WORKING AGENT declared something WORTH REMEMBERING in its verification
contract (a retrospective insight / non-obvious learning), or the settlement is
inherently notable (a user decision / discard-with-reason). Routine work leaves
nothing. There is no post-hoc extractor — a settle-time reader can't see tacit
knowledge. This package owns the stack-agnostic core (the shape + parsers + the
shared bar the ingest compiler still embeds).
"""

from backend.knowledge.extraction.worth_remembering import (
    WORTH_REMEMBERING_PRINCIPLE,
    RememberableKnowledge,
    is_inherently_notable,
    parse_declared_knowledge,
    parse_extraction,
)

__all__ = [
    "WORTH_REMEMBERING_PRINCIPLE",
    "RememberableKnowledge",
    "is_inherently_notable",
    "parse_declared_knowledge",
    "parse_extraction",
]
