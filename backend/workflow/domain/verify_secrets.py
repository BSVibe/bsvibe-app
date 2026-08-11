"""Secrets a PRODUCT declares for its own verification.

Which identity a browser probe signs in as, which key an HTTP probe presents,
which channel a delivery probe reads — every one of those is a fact about THAT
product. The platform does not get to choose them, and should not try: the same
reason the verification environment is DERIVED from what the repo declares
rather than hardcoded per stack, and the reason a product names its own image
instead of BSVibe shipping a test toolchain for everyone.

So the platform's whole job is a safe place to put them and a path to the check
that needs them.

**Stored on the product, and stored SEALED.** ``products.product_metadata`` is a
plaintext JSON column: it lands in DB dumps, in backups, in API responses, in
the occasional log line. A password written there in the clear is a password
published. Sealing at the write seam means the plaintext exists only inside the
request that set it.

Two silent failure modes this module exists to prevent:

* **The round-trip wipe.** A settings screen reads the product, shows the secret
  as a mask, and PUTs the whole object back. Taking that mask literally would
  destroy the secret on every unrelated edit — so the mask means "keep what is
  there", which is the only reading that makes a full-object PUT safe.
* **Double sealing.** Every write re-seals the whole map; encrypting the
  ciphertext again leaves a value nothing can decrypt, and nothing notices until
  a check needs it.

⚠️ The names are validated because they become ``docker run -e NAME``. A name
carrying a space or an ``=`` becomes a different flag — or a value assignment
that would drag the secret into the command string, which is the one place it
must never be (command strings are persisted on ``executor_tasks.prompt``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

#: Where a product keeps them, beside ``verify_stack`` and ``execution_target``.
METADATA_KEY = "verify_secrets"

#: Marks a value as already sealed. Explicit rather than "does it look like
#: base64", because guessing wrong in either direction is unrecoverable: a
#: double-encrypted secret can never be read, and a plaintext one mistaken for
#: ciphertext is a published password.
_SEALED_PREFIX = "enc:"

#: What a reader sees instead of the secret, and what a writer sends back to say
#: "leave it alone". One token for both because they are the same round-trip.
MASK = "***"

#: POSIX-ish environment variable names. Deliberately strict.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _secrets_of(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    raw = (metadata or {}).get(METADATA_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"{METADATA_KEY} must be a mapping of NAME -> value (got {type(raw).__name__})"
        )
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


def seal_secrets(
    metadata: Mapping[str, Any],
    *,
    encrypt: Callable[[str], str],
    prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``metadata`` with every declared secret stored encrypted.

    Idempotent: an already-sealed value passes through untouched, so re-writing
    a product does not re-encrypt what it already holds.

    ``prior`` is the product's current metadata, which is what makes the mask
    mean "keep this". Without it the mask has nothing to refer to and the key is
    dropped — a secret whose value is literally ``***`` is worse than none.

    An empty value removes the secret. That is the only way to unset one, and it
    is unambiguous in a way that "omit the key" is not (a partial update omits
    keys it never meant to touch).
    """
    incoming = _secrets_of(metadata)
    if METADATA_KEY not in metadata:
        return dict(metadata)

    kept = _secrets_of(prior)
    sealed: dict[str, str] = {}
    for name, value in incoming.items():
        if not _NAME_RE.match(name):
            raise ValueError(
                f"{METADATA_KEY} name {name!r} is not a valid environment variable name — "
                "it is passed to the check environment as `-e NAME`"
            )
        if value == MASK:
            # "Leave it alone" — and if there was nothing to leave, nothing.
            if name in kept:
                sealed[name] = kept[name]
            continue
        if not value:
            continue
        sealed[name] = (
            value if value.startswith(_SEALED_PREFIX) else _SEALED_PREFIX + encrypt(value)
        )

    out = dict(metadata)
    out[METADATA_KEY] = sealed
    return out


def redact_secrets(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """``metadata`` safe to hand to any reader: every secret shown as the mask.

    The ciphertext is the secret's only representation, and there is no reason
    for every reader of a product to hold it. The mask is also the token a
    writer sends back to keep the value — read and write are one round-trip.
    """
    out = dict(metadata)
    if METADATA_KEY in out:
        out[METADATA_KEY] = {name: MASK for name in _secrets_of(metadata)}
    return out


def declared_secret_names(metadata: Mapping[str, Any] | None) -> list[str]:
    """The NAMES a product declared, sorted.

    Sorted because they go into a command string, and a command that differs
    run to run is a command nobody can compare. Names only, never values: that
    separation is what keeps the values out of every persisted string.
    """
    try:
        return sorted(_secrets_of(metadata))
    except ValueError:
        # A malformed declaration is refused at the WRITE seam. A reader that
        # met one anyway must not take the run down over it.
        return []


def unseal_secrets(
    metadata: Mapping[str, Any] | None, *, decrypt: Callable[[str], str]
) -> dict[str, str]:
    """``{NAME: plaintext}`` for the check environment.

    Called at the last possible moment — where the values are handed to the
    dispatch channel that carries them to the founder's machine — and never
    stored, logged or interpolated into a command.

    A value that fails to decrypt is DROPPED rather than raised: the check that
    needs it will fail on its own terms (a login that does not work is a
    verdict), while a raise here would take down every run of a product whose
    KMS key rotated.
    """
    out: dict[str, str] = {}
    for name, value in _secrets_of(metadata).items():
        if not value.startswith(_SEALED_PREFIX):
            continue
        try:
            out[name] = decrypt(value[len(_SEALED_PREFIX) :])
        except Exception:  # noqa: BLE001, S112 — an unreadable secret is not a crash
            continue
    return out


__all__ = [
    "MASK",
    "METADATA_KEY",
    "declared_secret_names",
    "redact_secrets",
    "seal_secrets",
    "unseal_secrets",
]
