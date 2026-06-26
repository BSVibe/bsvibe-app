# E2E — R16 concept-centric report references

Founder: the knowledge graph shows promoted CONCEPTS; the report leaned on raw
SEEDLING notes (garden/seedling/settle-*). Decision: the graph (mature concepts)
is the main axis → make the report concept-centric.

Architecture found: SemanticNoteRetriever embeds/searches GARDEN seedlings only
(note_embeddings = garden/ only); CanonConceptRetriever resolves CONCEPTS (graph
anchors). The report's references mixed both; the graph shows only concepts.

## Backend (pytest)
- [x] references DROP raw seedling "Related note — garden/seedling/settle-*" hits
- [x] references KEEP concepts (Function/…) + prior decisions/rejections
- [x] the run's own written note still surfaces under `written` ("추가한 지식")
- [x] ruff + mypy + pytest 40; D35 LOC held (_narrative 181)

Note: the seedling semantic search still feeds the VERIFY contract (LLM context)
— this only trims the founder-facing report display.

## Prod dogfood (manual)
- [ ] Open a report → "참고한 지식" shows concepts + decisions, no raw settle-* notes
- [ ] Concept chips still open the concept viewer (R13/R15); "추가한 지식" still opens the note
