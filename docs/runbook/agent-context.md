# Where an agent session's rules and skills come from (#951)

Measured on 2026-10-20 instead of assumed. Re-measure after a Claude Code upgrade or an environment change.

| Session | User rules (`rules/*.md`) | Custom skills (~228) | Repo `CLAUDE.md` |
|---|---|---|---|
| Founder's Mac — terminal, desktop, or a **bridge** session (`environment_kind: bridge`) | ✅ from `~/.claude/rules` → symlink into `~/.claude/claude-skills` | ✅ `~/.claude/skills` → same | ✅ |
| **Cloud** session (claude.ai/code, `environment_kind: anthropic_cloud`) — before #951 | ❌ none in context | ❌ only the built-in/Anthropic skills | ❌ the repo had none |
| Cloud session — after #951 | ✅ installed and printed into context by the SessionStart hook | ✅ linked into `~/.claude/skills` by the hook | ✅ |
| Devcontainer (`.devcontainer/`) | ❌ no `~/.claude` mount, no Claude CLI installed | ❌ | ✅ if a CLI is run inside it |

## How the cloud cell was measured

A read-only cloud session was created on this repo and asked to report its own context.

- **Rules:** none of the user rules were in its context. The only user-level content was the account's preference text.
- **Skills:** its skill list held only built-ins. Neither `feature-workflow` nor `retrospective` was present.
- **Filesystem:**
  - `~/.claude` was the harness's own (`HOME=/root`).
  - `~/.claude/rules` did not exist.
  - `ls .claude` in the repo said "No such file or directory".
  - There was no `CLAUDE.md` anywhere at depth 3.

## The source of truth

`~/.claude/{skills,rules,commands,hooks,settings.json}` on the Mac are symlinks into `~/.claude/claude-skills`, the git repo `blas1n/claude-skills`.
- The repo is **public**.
- A launchd job (`com.claude.skills-autocommit`) commits and pushes it every 5 minutes, so nothing on the Mac is ever unpushed.

## How a cloud session gets them

`.claude/settings.json` registers `.claude/hooks/install-skills.sh` as a **SessionStart** hook.
- It acts only when `CLAUDE_CODE_REMOTE=true`. Mac and bridge sessions are untouched.
- It shallow-clones `blas1n/claude-skills` into `~/.claude/claude-skills`.
- It links `skills/`, `rules/` and `commands/` entry by entry. It never replaces what the harness already put there, and never touches `settings.json` or `hooks/`, which the cloud harness owns.
- It prints the rules into the session's context, because rules and skills may already have been read by the time the hook runs.
- Running it twice is safe. The `-L` check matters: one skill in the repo is a dangling symlink, and `-e` alone made the second run fail.

**Not given to cloud sessions:**
- the founder's `settings.json` and hooks (they belong to the Mac);
- MCP connectors (BSVibe's own MCP is a connector). Without it a cloud session must not write to GitHub on this repo — see `CLAUDE.md` §1.

## Devcontainer

`.devcontainer/` mounts only the repo and installs no Claude CLI.

`~/.claude/settings.json` points `GOOGLE_APPLICATION_CREDENTIALS` at `/home/vscode/.claude/credentials/…`, a path that only exists if the container user mounts their own `~/.claude`. Nothing in this repo provides it.
