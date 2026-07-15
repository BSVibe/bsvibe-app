# E2E — Unified run-scoped tool contract (INV-7 #1 + #2)

**What changed:** one factory (`assemble_run_tool_registry`) now builds the run's inner
`ToolRegistry` for BOTH the MCP transport (`build_run_tool_registry`) and the in-process loop
(`_drive_loop`), so the base tools + `knowledge_search` cannot drift between them. The dispatch
adapter's `WORK_TOOL_NAMES` (the CLI `--allowedTools` allowlist) is DERIVED from the single
`RUN_TOOL_FORWARDING` source, not hand-kept.

## Automated (must stay green)

- [x] `knowledge_search` is invokable through the production MCP factory — no `Unknown tool`
  (`tests/mcp/test_work_registry_factory.py::test_knowledge_search_is_invokable_through_the_factory`).
- [x] Every advertised forwarding work tool maps to an inner the shared factory registers — the
  drift class is impossible by construction
  (`tests/mcp/test_work_tools.py::test_every_forwarding_work_tool_maps_to_a_factory_registered_inner`).
- [x] `knowledge_search` runs end-to-end on the factory registry, degrading gracefully with no
  retriever (`...::test_knowledge_search_actually_runs_on_the_factory_registry`).
- [x] Advertised (`WORK_TOOL_NAMES`) ≡ server-offered run-scoped surface
  (`tests/mcp/test_run_scoped_tool_surface.py::test_a_run_scoped_token_sees_only_the_work_tools`).
- [x] Real-registry round-trips for `file_edit` / `declare_verification` (fake-registry stub
  replaced — INV-7 test discipline).
- [x] `lint-imports`: MCP context still depends only on Identity + Workflow + Knowledge + common
  (retriever build uses `backend.knowledge`, inside the allowlist).

## Manual / live (executor run against a real workspace)

- [ ] Dispatch an executor coding task to a workspace that HAS settled knowledge. Confirm the
  agent's `mcp__bsvibe__bsvibe_work_knowledge_search` call returns statements (not
  `ToolError: Unknown tool: 'knowledge_search'`) — executor RAG grounding restored.
- [ ] Same task on an EMPTY-knowledge workspace: `knowledge_search` returns "No settled
  knowledge found" / "No workspace knowledge is available" — graceful, never a 500 / never
  breaks the rest of the tool surface.
- [ ] Confirm the run still verifies: `declare_verification` → `file_write`/`file_edit` gate
  intact, `artifact_refs` populated, host repo byte-identical (no regression to T2b-2 state
  rehydration or the cross-tenant workspace guard).
- [ ] Worker `system/init` check passes: the CLI is offered exactly `WORK_TOOL_NAMES` and the
  server offers the same set to a run-scoped token (no `exposed - allowed` mismatch).

## Deferred (NOT in this lift — see PR body)

- [ ] `invoke_skill` over MCP — needs `SkillLoader` (`backend.extensions`) + a completion fn
  (`backend.router` resolver) injected from the composition root; still works on the worker path.
- [ ] Connector action tools over MCP — dynamic per-run names; needs run/workspace context in the
  static `--allowedTools` advertisement + overlaps INV-1 (connector registry).
