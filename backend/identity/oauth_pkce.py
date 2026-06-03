"""PKCE (RFC 7636) verification + RFC 8252 loopback redirect matching.

Pure functions, no I/O. The trickiest correctness in the OAuth flow
lives here:

* ``verify_pkce`` — S256-only constant-time comparison. We never accept
  ``plain`` because every supported client (CLI, Claude Code) supports
  SHA-256, and accepting ``plain`` is a foot-gun.
* ``match_redirect_uri`` — RFC 8252 §7.3 loopback rule: a client may
  register ``http://127.0.0.1/callback`` and the actual request URI
  ``http://127.0.0.1:54321/callback`` (port chosen by the CLI loopback
  listener) MUST match. Non-loopback URIs require exact string equality.
* ``redirect_uris_equivalent`` — same loopback equivalence applied to the
  ``/token`` redirect_uri cross-check.
"""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import urlsplit

_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]+$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def is_valid_verifier(verifier: str) -> bool:
    """RFC 7636 §4.1 — 43..128 chars of the URL-safe verifier alphabet."""
    if not 43 <= len(verifier) <= 128:
        return False
    return bool(_VERIFIER_RE.match(verifier))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def verify_pkce(challenge: str, method: str, verifier: str) -> bool:
    """RFC 7636 §4.6 — S256-only constant-time-ish comparison.

    ``verifier`` is hashed SHA-256 and base64url-encoded, then compared
    against the stored ``challenge``. Returns ``False`` on length
    mismatch (no oracle) and on any character difference (XOR-fold).
    """
    if method != "S256":
        return False
    if not is_valid_verifier(verifier):
        return False
    computed = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    if len(computed) != len(challenge):
        return False
    diff = 0
    for a, b in zip(computed, challenge, strict=True):
        diff |= ord(a) ^ ord(b)
    return diff == 0


def match_redirect_uri(registered: list[str], requested: str) -> bool:
    """Whether ``requested`` is a valid redirect_uri for a client whose
    registered list is ``registered``.

    Exact-string match always wins. Otherwise the RFC 8252 §7.3 loopback
    rule applies: if both registered + requested use a loopback host
    (``127.0.0.1`` / ``localhost`` / ``[::1]``) on the same scheme and
    path, the port may differ.
    """
    if requested in registered:
        return True
    try:
        req = urlsplit(requested)
    except ValueError:
        return False
    if req.scheme != "http":
        return False
    if req.hostname not in _LOOPBACK_HOSTS:
        return False
    for r in registered:
        try:
            reg = urlsplit(r)
        except ValueError:
            continue
        if reg.scheme != req.scheme:
            continue
        if reg.hostname not in _LOOPBACK_HOSTS:
            continue
        if reg.path != req.path:
            continue
        return True
    return False


def redirect_uris_equivalent(stored: str, supplied: str) -> bool:
    """Compare the redirect_uri stored on the authorization code against
    the one supplied at ``/token`` exchange.

    Per RFC 8252 §7.3 loopback hostnames refer to the same endpoint and
    are interchangeable; the path + scheme + port must still match.
    Non-loopback URIs require exact equality.
    """
    if stored == supplied:
        return True
    try:
        s = urlsplit(stored)
        r = urlsplit(supplied)
    except ValueError:
        return False
    if s.scheme != r.scheme or s.path != r.path or s.query != r.query:
        return False
    if s.port != r.port:
        return False
    if s.hostname not in _LOOPBACK_HOSTS or r.hostname not in _LOOPBACK_HOSTS:
        return False
    return True


__all__ = [
    "is_valid_verifier",
    "match_redirect_uri",
    "redirect_uris_equivalent",
    "verify_pkce",
]
