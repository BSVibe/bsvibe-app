#!/bin/sh
# bsvibe worker installer — GitHub-Actions-runner-style one-liner.
#
#   curl -fsSL https://api.bsvibe.dev/install-worker.sh | sh
#
# Installs the ``bsvibe`` + ``bsvibe-worker`` CLIs onto this host (from the
# public source repo, via uv), then prints the two commands that register and
# run the worker. Idempotent: re-running updates an existing install in place.
# No secret is handled here — registration authenticates interactively via
# ``bsvibe login`` afterwards.
set -eu

REPO="${BSVIBE_WORKER_REPO:-https://github.com/BSVibe/bsvibe-app.git}"
BSVIBE_HOME="${BSVIBE_HOME:-$HOME/.bsvibe}"
SRC="$BSVIBE_HOME/worker-src"
BIN="$BSVIBE_HOME/bin"
SERVER_URL="${BSVIBE_WORKER_SERVER_URL:-https://api.bsvibe.dev}"

say() { printf '%s\n' "bsvibe-installer: $*" >&2; }
die() { say "ERROR: $*"; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required (install Xcode CLT or your distro's git)."
command -v curl >/dev/null 2>&1 || die "curl is required."

# 1. uv — the Python toolchain the worker runs under. Install if missing.
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv (astral) ..."
  curl -fsSL https://astral.sh/uv/install.sh | sh
  # uv's installer drops the binary in one of these; make it visible NOW.
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && PATH="$d:$PATH"
  done
  export PATH
fi
command -v uv >/dev/null 2>&1 || die "uv install did not land on PATH; re-open your shell and re-run."

# 2. Clone or update the source the worker runs from.
mkdir -p "$BSVIBE_HOME"
if [ -d "$SRC/.git" ]; then
  say "updating $SRC ..."
  git -C "$SRC" fetch --depth 1 origin HEAD
  git -C "$SRC" reset --hard FETCH_HEAD
else
  say "cloning $REPO -> $SRC ..."
  git clone --depth 1 "$REPO" "$SRC"
fi

# 3. Resolve dependencies (the worker imports the app package).
say "resolving dependencies (uv sync) ..."
( cd "$SRC" && uv sync )

# 4. Wrapper commands on PATH — ``bsvibe`` / ``bsvibe-worker`` run the pinned
#    source tree via uv, matching how the operator's own hosts run.
mkdir -p "$BIN"
for cmd in bsvibe bsvibe-worker; do
  cat > "$BIN/$cmd" <<WRAP
#!/bin/sh
exec uv run --directory "$SRC" $cmd "\$@"
WRAP
  chmod +x "$BIN/$cmd"
done

say "installed: bsvibe, bsvibe-worker -> $BIN"

# 5. Next steps. Print the exact register+run command for THIS deployment.
cat >&2 <<NEXT

  ✅ Installed. Add the CLIs to your PATH, then register + run this host:

      export PATH="$BIN:\$PATH"
      BSVIBE_WORKER_SERVER_URL=$SERVER_URL bsvibe login \\
        && bsvibe-worker register --name "\$(hostname)" \\
        && bsvibe-worker run

  ``bsvibe login`` opens your browser once to authenticate the host; register
  saves a per-worker token at ~/.bsvibe/worker.token. No token to paste.
NEXT
