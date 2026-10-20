#!/usr/bin/env bash
# SessionStart hook — give a CLOUD Claude Code session the founder's skills and rules (#951).
#
# Measured 2026-10-20: a claude.ai/code cloud session on this repo saw none of the user rules
# (TDD, Python architecture/testing/security, retrospective) and none of the ~228 custom skills —
# they live in ~/.claude on the founder's Mac (symlinks into the public repo
# blas1n/claude-skills). Sessions bridged to the Mac already see them, so this does nothing
# there: it acts only when Claude Code says it is running remotely.
#
# Only skills/, rules/ and commands/ are linked. The cloud harness owns ~/.claude/settings.json
# and its hooks; replacing those would break the session it runs in.
set -euo pipefail

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

src="$HOME/.claude/claude-skills"
if [ ! -d "$src/.git" ]; then
  git clone --quiet --depth 1 https://github.com/blas1n/claude-skills "$src" || {
    echo "install-skills: could not fetch blas1n/claude-skills; continuing without it" >&2
    exit 0
  }
fi

for part in skills rules commands; do
  [ -d "$src/$part" ] || continue
  mkdir -p "$HOME/.claude/$part"
  for entry in "$src/$part"/*; do
    name=$(basename "$entry")
    # Never shadow what the harness already put there — and ``-L`` too, because ``-e`` is false
    # for a symlink whose target is missing, which made a second run fail on "File exists".
    target="$HOME/.claude/$part/$name"
    [ -e "$target" ] || [ -L "$target" ] || ln -s "$entry" "$target"
  done
done

# Skills and rules are read when the session starts, possibly before this hook ran. The rules
# are short and are the part that must not be missed, so they also go into this session's
# context through the hook's output.
if [ -d "$src/rules" ]; then
  echo "User rules (from blas1n/claude-skills/rules — installed by .claude/hooks/install-skills.sh):"
  for f in "$src/rules"/*.md; do
    echo
    cat "$f"
  done
fi
