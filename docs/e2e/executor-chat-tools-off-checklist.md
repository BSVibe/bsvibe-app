# E2E — chat turns disable the executor's own tools (INV-7 #6, codex + opencode)

`claude_code` already does this (`--disallowedTools "*"`). This lift extends the
same guarantee to the other two executor CLIs: a CHAT turn (no BSVibe tools —
frame stage / judge / knowledge ingest / decision-question authoring) must run
with the CLI's OWN tools/filesystem access OFF and answer in a single turn.
Otherwise the CLI goes agentic, explores its empty per-task temp dir, and answers
"the directory is empty" — burning the 300 s budget (founder's #1 recurring pain).

## Verified CLI contracts (this machine, 2026-07-15)

- **opencode 1.17.3** — `POST /session/{id}/message` has a `tools` field
  (`{toolName: boolean}`, from the daemon's own OpenAPI `/doc`). The `"*"`
  wildcard turns EVERY tool off. **Live probe** (empty temp dir, "List the files
  in the current working directory."):
  - tools ON (no `tools` key): model ran `bash: ls` and replied *"The current
    working directory (`/private/tmp/ocprobe`) is empty."* — the exact bug.
  - `tools: {"*": false}`: NO tool executed. → chat branch (a): send
    `tools: {"*": false}`.
- **codex 0.130.0** — `codex exec` flags (from the binary's own help strings):
  `--sandbox {read-only|workspace-write|danger-full-access}`, `--ask-for-approval`,
  `--skip-git-repo-check`, `--model`, `--config`, `--output-schema`. NONE disables
  the shell/exec tool; `--sandbox read-only` only blocks WRITES (the model still
  runs read commands to explore). No honest tools-off mode → chat branch (b):
  REFUSE the chat turn loudly (terminal error chunk), never run agentic.

## Automated (RED→GREEN, in CI)

- [x] `test_opencode.py::test_chat_turn_disables_all_tools` — `agentic=False` →
      body `tools == {"*": False}`.
- [x] `test_opencode.py::test_agent_run_keeps_its_tools` — `agentic=True` → no
      `tools` key (full tool set kept).
- [x] `test_opencode.py::test_missing_agentic_defaults_to_agent_run` — back-compat.
- [x] `test_codex.py::test_chat_turn_is_refused_not_run_agentic` — `agentic=False`
      → terminal error chunk, subprocess NEVER spawned.
- [x] `test_codex.py::test_agent_run_still_spawns_agentic` — `agentic=True` →
      agentic `codex exec` still runs.
- [x] `test_codex.py::test_missing_agentic_defaults_to_agent_run` — back-compat.

## Manual live-run (per executor, on the Mac Mini worker)

Precondition: a workspace whose default routing points at the target executor
account, with a working model credential for that provider.

### opencode
- [ ] Dispatch a chat-shaped turn (e.g. a frame-stage question / knowledge ingest)
      routed to the opencode account. Ask something like "현 프로젝트 상황 설명해줘".
- [ ] Confirm the answer draws on the injected grounding, NOT "the directory is
      empty" / a description of an empty temp dir.
- [ ] Confirm it returns in one turn (well under the 300 s budget), no tool calls
      in the transcript.
- [ ] Confirm an AGENTIC coding run routed to opencode is unchanged (still edits /
      runs bash in its sandbox).

### codex
- [ ] Dispatch a chat-shaped turn routed to the codex account.
- [ ] Confirm it comes back as a LOUD terminal error naming codex's tools-off
      limitation (not a silent agentic answer about an empty dir, not a 300 s hang).
- [ ] Confirm an AGENTIC `codex` task is unchanged (still runs `codex exec`).
