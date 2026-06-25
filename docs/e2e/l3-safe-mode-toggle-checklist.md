# E2E — L3 Safe / Auto mode toggle (#5)

Founder pain: no way to switch between Safe Mode (every deliverable waits for
approval) and Auto (deliverables auto-dispatch), so every task needed a manual
approve/decline.

Note: the delivery gate ALREADY reads `workspaces.safe_mode`
(`delivery_worker.resolve_output_mode_gate`) — Auto worked at the backend; what
was missing was a way to flip the flag. Founder decision (2026-06-25): Auto = the
delivery gate only (Claude-Code bypass-permission UX); real blocks
(ask_user_question / verification failures) still surface as Decisions.

## Backend (automated)
- [x] `GET /api/v1/workspace` returns `safe_mode` (`tests/api/test_v1_workspace.py`)
- [x] `PATCH /api/v1/workspace {safe_mode:false}` persists; a name-only PATCH leaves it alone
- [x] MCP `bsvibe_safe_mode_get` returns the flag (`tests/mcp/test_safe_mode_and_direct.py`)
- [x] MCP `bsvibe_safe_mode_set` flips it; requires `mcp:write` scope

## PWA (automated — `apps/pwa/test/general-tab.test.tsx`)
- [x] Settings → General renders a Safe / Auto segmented control reflecting the loaded mode
- [x] switching to Auto PATCHes `{safe_mode:false}` (optimistic, reverts on failure)

## Prod dogfood (manual — verify at final review)
- [ ] Settings → General → switch to Auto → submit a product run that delivers →
      the deliverable ships WITHOUT a Safe Mode approval gate.
- [ ] Switch back to Safe → a new delivery is held in the Decisions/Safe Mode queue.
- [ ] An MCP client calls `bsvibe_safe_mode_set {safe_mode:false}` → the PWA toggle reflects Auto.
- [ ] In Auto, a run that hits a real `ask_user_question` / verification failure STILL
      raises a Decision (Auto only bypasses the delivery gate, not real blocks).
