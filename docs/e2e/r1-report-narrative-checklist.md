# E2E — R1 Report "what this did" narrative

The redesigned report leads with a plain-language "what this did" instead of the
raw changed-file list. A chat model composes it from the intent + captured diff,
lazily on first view, cached on the deliverable.

## Backend (automated)
- [x] `ReportNarrativeService.narrate` composes from intent + summary + diff (`tests/glue/test_report_narrative.py`)
- [x] returns `None` when no chat model resolves (report falls back to the request line)
- [x] `GET /deliverables/{id}/report` returns a cached `narrative` without re-generating
- [x] a verified deliverable with no cached narrative lazily generates + caches it (chat stubbed)

## Prod dogfood (manual — verify at final review, once R3 frontend lands)
- [ ] Open a verified deliverable's report → a plain-language "what this did" sentence appears
      (not the raw file list), in the workspace language.
- [ ] Re-open the same report → instant (served from cache, no re-generation).
- [ ] A deliverable on a workspace with no chat model → report still loads, narrative absent.
