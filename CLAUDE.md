# bsvibe-app — rules for agents working in this repo

Read these before your first write. Each one exists because breaking it has already cost a
production incident or a lost day. The current state of the work is in `docs/HANDOFF.md`;
how the system fits together is in `docs/SYSTEM_OVERVIEW.md`.

## 1. A GitHub write on this repo starts a production run

BSVibe watches this repository through its own GitHub binding. Opening, updating or merging a
PR, pushing to an open PR, and creating or commenting on an issue all become **work for the
production system**: a real run, real tokens, and sometimes a question to the founder.

- Before any GitHub write, pause the product binding's trigger filter (BSVibe MCP
  `bsvibe_bindings_update`). Do the write. **Wait for intake to reject the event** (≥ 90 s; a
  merge sends no event), check that `bsvibe_runs_list` shows no new run, then restore the filter.
  Restoring right after the write is the bug: the filter is applied when intake processes the
  stored event, not when the webhook arrives.
- Batch all writes into one pause window.
- **If you cannot reach BSVibe's MCP tools** (for example in a cloud session without that
  connector), do not write to GitHub on this repo. Leave the branch pushed only if asked, and
  hand the PR and issue writes back to the founder.

## 2. The repository is public

Commit messages, PR titles and bodies are world-readable before a fix deploys. For security
fixes, write neutral titles and keep reproduction steps, internal hostnames and identifiers out
of GitHub. Never commit secrets, tokens, `.env` contents or monitoring URLs.

## 3. Production data is off limits to agents

- Do not read or exec into production containers or the production database. `docker logs` on the
  production context is the sanctioned window.
- `tests/_support.db_engine` **deletes every row** of whatever `BSVIBE_DATABASE_URL` points at.
  Verify on Postgres only with CI or a throwaway container, never an existing database.

## 4. How changes are made

- Test first (red for the right reason), then the code. For a guard, **cut the production edge it
  guards and watch a test go red** — a guard that stays green when its wire is cut guards nothing.
- Backend: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy backend`,
  `uv run lint-imports`, `uv run pytest`.
- PWA (`apps/pwa`): `npx --yes pnpm@9 lint`, `… exec tsc --noEmit`, `… test`.
- `main` requires an up-to-date branch: rebase on `origin/main` before the first push.
- Commit messages: `type(scope): summary` with a short bullet body. No `Co-Authored-By` line.
- Documents in git are written in English.

## 5. Where a session's context comes from

Sessions on the founder's machine get the user rules and skills from `~/.claude`. Cloud sessions
get them from `.claude/hooks/install-skills.sh` (a SessionStart hook that installs the public
`blas1n/claude-skills` repo and prints its rules into the session). See `docs/runbook/agent-context.md`.
