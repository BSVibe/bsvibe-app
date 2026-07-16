# E2E — Knowledge "Correct" is honestly unavailable (B5)

The in-place field-rewrite editor for a knowledge note ("Correct") was never
built: no vault writer primitive rewrites whitelisted fields, and the
`corrections` payload is dropped at every layer. The previous behaviour minted
a correction row, showed the founder "Corrected X" + a 30-second undo, and (on
window expiry) emitted an `ontology.correction.applied` audit event — a false
success confirmation and a false audit record for an operation that changed
nothing.

This lift makes the surface honest: **Correct is refused, Retract still works.**
No false success, no false `ontology.correction.applied` audit.

## Backend — RetractionService (application layer)

- [ ] `issue(action="correct")` raises `CorrectionUnavailableError`; no
      `ontology_corrections` row is persisted and no audit event is emitted.
- [ ] `issue(action="retract")` is unchanged — row persisted, undo window opens,
      `ontology.correction.requested` emitted.
- [ ] `apply_pending` never selects a `correct` row (legacy rows past their
      deadline are ignored): `applied` count excludes them, `applied_at` stays
      NULL, and no `ontology.correction.applied` is emitted for them.
- [ ] Retract apply still writes the frontmatter tombstone and emits
      `ontology.correction.applied` (the audit is TRUE — the vault changed).

## Backend — REST (`/api/v1/inside`)

- [ ] `POST /api/v1/inside/nodes/{node_ref}/correct` returns `501 Not
      Implemented` with detail "correction (in-place field rewrite) is not
      available yet". The note file on disk is untouched.
- [ ] `POST /api/v1/inside/nodes/{node_ref}/retract` still returns `200` with the
      signal + undo window.
- [ ] `POST /api/v1/inside/corrections/{id}/undo` still works for a retract.

## Backend — MCP tools

- [ ] `bsvibe_knowledge_correct` raises a `ToolError` ("... not available yet")
      for a `mcp:write` caller — no signal returned, no row, no audit.
- [ ] `bsvibe_knowledge_correct` still enforces `mcp:write` scope (a `mcp:read`
      caller is rejected on scope before reaching the handler).
- [ ] `bsvibe_knowledge_retract` + `bsvibe_knowledge_undo_correction` unchanged.
- [ ] The tool's description states it is NOT available yet (discovery is honest).

## PWA — Inside view inspector (InspectorActions)

- [ ] Selecting a knowledge node shows a **disabled** "Correct" button with a
      "coming soon" tooltip (`Editing a note (Correct) is coming soon.` / KO:
      `노트 편집(정정)은 준비 중이에요.`).
- [ ] Clicking the disabled Correct button does nothing: no modal opens, no
      network request fires, no "Corrected X" toast appears, no undo countdown.
- [ ] The "Retract" button still opens the retract modal, POSTs, and shows the
      30-second undo toast; Undo still works.
- [ ] Locale toggle (en ↔ ko) shows the correct tooltip string in each language.

## Regression guardrails

- [ ] No code path writes `ontology.correction.applied` for a correction that
      changed no vault content.
- [ ] Retract, the two-role DB, and verification are untouched.
