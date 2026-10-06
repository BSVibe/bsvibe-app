"""The off-box probe must name WHICH layer broke — 게이트 3.

PR #918 watched prod from GitHub's runners so a dead Mac Mini could not go
unnoticed. It probed ``GET /api/health`` only, and that route is
``return HealthResponse(status="ok", version=…, git_sha=…)`` — settings in,
settings out. It touches no database and no Supabase.

On 2026-09-11 Supabase was paused and login was broken; ``/api/health`` answered
200 through the whole outage. A watch that green-lights an app whose auth is
dead is worse than none: it converts an outage into confidence. (Prior art in
this repo: a paused Supabase vanishing from DNS took ``login`` to 500 and went
unnoticed for weeks.)

So the probe takes TWO readings and reports the layer:

* **shallow** ``GET /api/health`` — edge + tunnel + app process
* **deep** ``GET /api/health/auth`` — the same plus the auth dependency: the
  backend relays GoTrue's own ``/auth/v1/health``. Only ``200`` is healthy.

  (Until 2026-10-06 the deep reading POSTed throwaway credentials to
  ``/api/auth/login`` and called the ``4xx`` healthy. Every reading was a failed
  sign-in: a ``supabase_token_failed`` warning in prod once a minute, and a share
  of the per-IP sign-in rate limit every user's login is proxied through.)

Verdicts are a pure function of the two codes, tested here across every branch.
The logic lives in ``.github/scripts/offbox-probe.sh`` rather than inline in the
workflow YAML precisely so it CAN be tested: a verdict nobody can reach from a
test is a verdict that is never checked.
"""

from __future__ import annotations

import http.server
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "offbox-probe.sh"


class _Stub(http.server.BaseHTTPRequestHandler):
    """Serves whatever code the test asked for, per path.

    ``edge_challenge`` makes it answer the way Cloudflare actually did on
    2026-09-11: 403 WITH a ``cf-mitigated`` header. That header is the whole
    distinction — a bare 403 is the app refusing, which is a different verdict.
    """

    codes: dict[str, int] = {}
    edge_challenge: bool = False
    hits: list[tuple[str, str]] = []

    def _respond(self) -> None:
        self.hits.append((self.command, self.path.split("?")[0]))
        code = self.codes.get(self.path.split("?")[0], 404)
        self.send_response(code)
        if self.edge_challenge:
            self.send_header("cf-mitigated", "challenge")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"probe":true}')

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence the stub
        return


@pytest.fixture
def stub() -> Iterator[tuple[str, dict[str, int]]]:
    """A local origin whose per-path status codes the test controls."""
    codes: dict[str, int] = {}
    _Stub.codes = codes
    _Stub.edge_challenge = False
    _Stub.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", codes
    finally:
        server.shutdown()
        server.server_close()


def _run(base_url: str) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-local URL
        ["bash", str(SCRIPT)],  # noqa: S607
        env={"BASE_URL": base_url, "PATH": "/usr/bin:/bin:/usr/local/bin", "RETRY_DELAY": "0"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


HEALTH = "/api/health"
AUTH = "/api/health/auth"


def test_all_healthy_is_green(stub: tuple[str, dict[str, int]]) -> None:
    """200 + 200 — the app answers and its auth dependency answered too."""
    base, codes = stub
    codes[HEALTH] = 200
    codes[AUTH] = 200
    rc, out = _run(base)
    assert rc == 0, out
    assert "OK" in out


def test_the_deep_reading_never_signs_in(stub: tuple[str, dict[str, int]]) -> None:
    """The whole reason for the auth health route — no failed sign-in per reading."""
    base, codes = stub
    codes[HEALTH] = 200
    codes[AUTH] = 200
    rc, out = _run(base)
    assert rc == 0, out
    assert ("GET", AUTH) in _Stub.hits
    assert not any(path == "/api/auth/login" for _m, path in _Stub.hits)


def test_a_4xx_on_the_deep_probe_is_not_healthy(stub: tuple[str, dict[str, int]]) -> None:
    """The deep route answers 200 or 503 — a 404 means it is not there, not "fine".

    The login-based reading had to call 4xx healthy (invalid credentials was the
    expected verdict). Carrying that over would green-light a backend that lost
    the route.
    """
    base, codes = stub
    codes[HEALTH] = 200
    codes[AUTH] = 404
    rc, out = _run(base)
    assert rc != 0, out


def test_app_up_but_dependency_down_is_the_case_that_bit_us(
    stub: tuple[str, dict[str, int]],
) -> None:
    """200 + 503 — Supabase paused. The old single-probe watch called this GREEN."""
    base, codes = stub
    codes[HEALTH] = 200
    codes[AUTH] = 503
    rc, out = _run(base)
    assert rc != 0, out
    assert "DEPENDENCY" in out
    # The alert must say the app itself is fine, or the first move is to restart
    # a backend that was never the problem.
    assert "/api/health" in out and "200" in out


def test_app_down_names_the_app(stub: tuple[str, dict[str, int]]) -> None:
    """A 502 on health is the app/tunnel, not the dependency."""
    base, codes = stub
    codes[HEALTH] = 502
    codes[AUTH] = 502
    rc, out = _run(base)
    assert rc != 0, out
    assert "APP" in out
    assert "DEPENDENCY" not in out


def test_a_cloudflare_challenge_is_named_as_the_edge(stub: tuple[str, dict[str, int]]) -> None:
    """403 from the edge is not an outage of the app — it is a WAF verdict.

    Measured 2026-09-11: every datacenter IP gets ``403 cf-mitigated: challenge``
    on this host while a residential IP gets 200. An alert that said "prod is
    down" would send someone to restart a perfectly healthy backend.
    """
    base, codes = stub
    codes[HEALTH] = 403
    codes[AUTH] = 403
    _Stub.edge_challenge = True
    rc, out = _run(base)
    assert rc != 0, out
    assert "EDGE" in out


def test_nothing_answers_at_all_is_unreachable(stub: tuple[str, dict[str, int]]) -> None:
    """000 on both — the box, the tunnel, or DNS. The reason the watch is off-box."""
    base, _codes = stub
    # Point at a closed port on localhost: connection refused → curl writes 000.
    closed = base.rsplit(":", 1)[0] + ":9"
    rc, out = _run(closed)
    assert rc != 0, out
    assert "UNREACHABLE" in out


def test_the_verdicts_are_distinct(stub: tuple[str, dict[str, int]]) -> None:
    """Four failure modes must not collapse into one word.

    The whole point of the second probe is telling them apart; a script that
    printed the same sentence for all of them would pass every other test here
    while restoring exactly the ambiguity this change exists to remove.
    """
    base, codes = stub
    seen = set()
    for health, login, edge in ((200, 500, False), (502, 502, False), (403, 403, True)):
        codes[HEALTH] = health
        codes[AUTH] = login
        _Stub.edge_challenge = edge
        _rc, out = _run(base)
        verdict = next(w for w in ("DEPENDENCY", "APP", "EDGE") if w in out)
        seen.add(verdict)
    assert seen == {"DEPENDENCY", "APP", "EDGE"}


def test_a_bare_403_is_the_app_not_the_edge(stub: tuple[str, dict[str, int]]) -> None:
    """Without ``cf-mitigated`` a 403 is the ORIGIN refusing — a different fix.

    The header is the only thing separating "a WAF bounced this client" from
    "the app rejected this request". Matching on the status code alone would
    mislabel every origin-side 403 as an edge problem.
    """
    base, codes = stub
    codes[HEALTH] = 403
    codes[AUTH] = 403
    _Stub.edge_challenge = False
    rc, out = _run(base)
    assert rc != 0, out
    assert "APP" in out
    assert "EDGE" not in out
