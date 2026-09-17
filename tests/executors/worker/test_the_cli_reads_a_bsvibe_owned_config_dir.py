"""The spawned CLI must not read the operator's ``~/.claude`` at all (#978).

``--setting-sources ""`` buys **content** isolation and nothing else: measured,
it takes the host's 184 user skills to 0. What it does not buy is **filesystem
reach**, and that distinction is not academic — it is what stopped prod.

#965: the host's ``~/.claude/plugins/known_marketplaces.json`` held a
devcontainer path (``/home/vscode/...``). On this machine ``/home`` is an autofs
mount, so the automounter grabbed the CLI as it resolved that path and never let
go. Every run in prod timed out in framing. **BSVibe's code was correct.** The
CLI never *used* that setting — it only resolved the path — which is exactly why
"plugins: []" was never evidence of isolation.

The one lever left is ``CLAUDE_CONFIG_DIR``. It was measured on 2026-09-16 and
rejected: it returned ``Not logged in``. That verdict is now **wrong**, and the
2×2 says why — the worker's own OAuth credential was expired that day, so the
CLI was authenticating off the host's credential file, which lives in the very
directory the redirect moves:

===================  ======================  =========================
                     host config dir         BSVibe config dir
===================  ======================  =========================
token injected       authenticates           **authenticates**
token absent         authenticates (host)    ``Not logged in``
===================  ======================  =========================

⚠️ So this fix is only safe while ``ANTHROPIC_AUTH_TOKEN`` is injected — which is
the lifeline these tests pin. ``ensure_claude_bearer`` resolves it from the
worker's own credential and, failing that, borrows the interactive CLI's; both
reads happen **in our Python**, via ``Path.home()``, never through the CLI's
config dir. Measured end-to-end with a deliberately burned worker credential: the
fallback token still authenticates a redirected CLI.

What stays reachable is ``/Library/Application Support/ClaudeCode`` (9 probes,
unchanged). That path does not exist on this host, and writing it needs root — a
different risk class from ``~/.claude``, which any local tool can edit. Stated
rather than papered over: the claim in this module's comments is "the config
directory is BSVibe's", not "the CLI touches no host path".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.executors.worker import claude_code


def _env(monkeypatch: pytest.MonkeyPatch, **settings: Any) -> dict[str, str]:
    """Build the spawn env the way the worker does, with settings overridden."""
    from backend.executors.worker import config as worker_config

    base = worker_config.WorkerSettings(**settings)
    monkeypatch.setattr(claude_code, "get_worker_settings", lambda: base, raising=False)
    monkeypatch.setattr(worker_config, "get_worker_settings", lambda: base, raising=False)
    return claude_code._subprocess_env_with_bearer()


def test_the_cli_is_pointed_at_a_bsvibe_owned_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claude-config"
    env = _env(monkeypatch, claude_config_dir=str(target))

    assert env.get("CLAUDE_CONFIG_DIR") == str(target), (
        "without this the CLI resolves paths under the operator's ~/.claude — the "
        "reach that stopped prod in #965"
    )
    assert target.is_dir(), "the CLI will not create it; an absent dir is not a redirect"


def test_the_config_dir_is_never_the_operators_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposition, stated as the thing that must not happen.

    A default that resolved back under ``~/.claude`` would satisfy "the variable
    is set" while changing nothing at all.
    """
    env = _env(monkeypatch, claude_config_dir=str(tmp_path / "cfg"))
    configured = Path(env["CLAUDE_CONFIG_DIR"]).resolve()
    host = (Path.home() / ".claude").resolve()

    assert configured != host and host not in configured.parents, (
        f"{configured} still lives under the operator's harness"
    )


def test_the_default_is_owned_by_bsvibe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset must be SAFE, not host. An operator who configures nothing is the
    common case, and it is the case #965 happened in.

    Stated as *"a sibling of the state directory BSVibe already owns"* rather
    than by spelling ``.bsvibe``: this suite redirects ``BSVIBE_HOME`` to a
    temp dir, so a literal spelling pins the fixture instead of the proposition
    — and would then have to be loosened, which is how a guard stops guarding.
    """
    from backend.executors.worker.config import default_sandbox_cwd

    env = _env(monkeypatch)
    configured = Path(env["CLAUDE_CONFIG_DIR"])

    assert configured.parent == default_sandbox_cwd().parent, (
        f"default landed outside BSVibe's own state root: {configured}"
    )
    assert (Path.home() / ".claude").resolve() not in configured.resolve().parents


def test_the_bearer_is_still_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifeline. With the config dir moved, the injected token is the ONLY
    thing that authenticates — the measured 2×2 above is the whole argument.

    A change that set the config dir but dropped the token would produce
    ``Not logged in`` on every turn, i.e. a total executor outage.
    """
    monkeypatch.setattr(claude_code, "ensure_claude_bearer", lambda: "BORROWED-TOKEN")
    env = _env(monkeypatch, claude_config_dir=str(tmp_path / "cfg"))

    assert env.get("ANTHROPIC_AUTH_TOKEN") == "BORROWED-TOKEN"
    assert env.get("CLAUDE_CONFIG_DIR")


def test_an_uncreatable_dir_falls_back_to_the_host_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail OPEN, and say so.

    Refusing to spawn would convert a filesystem hiccup into a total outage —
    strictly worse than today's behaviour, which this fix is an improvement on.
    But a silent fallback is the trap where a degraded read becomes a
    measurement: the harness would quietly be the operator's again with nothing
    anywhere saying so. One loud line per turn, greppable.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def __getattr__(self, level: str) -> Any:
            def _rec(event: str, **kw: Any) -> None:
                captured.append((event, kw))

            return _rec

    monkeypatch.setattr(claude_code, "logger", _Log())

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")  # mkdir under a FILE raises
    env = _env(monkeypatch, claude_config_dir=str(blocker / "cfg"))

    assert "CLAUDE_CONFIG_DIR" not in env, (
        "pointing the CLI at a directory that does not exist is worse than not pointing it anywhere"
    )
    assert any("config_dir" in event for event, _ in captured), (
        f"fell back to the operator's harness in silence; logged {captured}"
    )


def test_the_control_is_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pairs with the test above: the same helper, a creatable dir, variable set.

    Without this the fallback test passes just as well on a build that never
    sets the variable at all.
    """
    env = _env(monkeypatch, claude_config_dir=str(tmp_path / "fine"))
    assert "CLAUDE_CONFIG_DIR" in env
