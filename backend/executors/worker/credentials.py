"""Credential file management for the BSVibe worker CLI — Lift E4.

The Lift E4 worker UX is GitHub-Actions-runner-shaped: one host-side
``bsvibe login`` (PKCE loopback) writes the founder's OAuth credentials to a
well-known path, then ``bsvibe-worker register`` reads them and registers a
worker against the backend using ``Authorization: Bearer <access_token>``.

Two paths live here:

* ``~/.config/bsvibe/credentials.json`` — what ``bsvibe login`` writes.
  Shape::

      {
          "access_token": "...",
          "refresh_token": "...",     # optional
          "expires_at": 1717999999,    # optional, unix seconds
          "issuer": "https://auth.bsvibe.dev",
          "obtained_at": 1717900000
      }

  Mode 0600 (founder-only) on write. The CLI's ``register`` step reads it
  and falls back to ``BSVIBE_ACCESS_TOKEN`` (env) for CI / headless hosts.
* ``~/.bsvibe/worker.token`` — what ``bsvibe-worker register`` writes after
  the backend returns the per-worker token. One line, mode 0600.

Both paths are XDG-friendly: ``XDG_CONFIG_HOME`` overrides the credentials
parent if set, and ``BSVIBE_HOME`` overrides the worker-token parent. Tests
inject temp paths via the overrides.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_CREDENTIALS_REL = Path("bsvibe/credentials.json")
_DEFAULT_WORKER_TOKEN_REL = Path("worker.token")
_DEFAULT_WORKER_CONFIG_REL = Path("config.json")


class CredentialsNotFound(Exception):
    """Raised when no host OAuth credential can be located."""


@dataclass(frozen=True)
class HostCredentials:
    """The host-side OAuth credentials ``bsvibe login`` produced."""

    access_token: str
    refresh_token: str | None
    expires_at: int | None
    issuer: str | None


def default_credentials_path() -> Path:
    """Return ``$XDG_CONFIG_HOME/bsvibe/credentials.json`` (or HOME fallback)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / _DEFAULT_CREDENTIALS_REL


def default_worker_token_path() -> Path:
    """Return ``$BSVIBE_HOME/worker.token`` (or ``~/.bsvibe/worker.token``)."""
    base = os.environ.get("BSVIBE_HOME")
    root = Path(base) if base else Path.home() / ".bsvibe"
    return root / _DEFAULT_WORKER_TOKEN_REL


def load_host_credentials(path: Path | None = None) -> HostCredentials:
    """Read and validate the credentials file written by ``bsvibe login``.

    Falls back to the ``BSVIBE_ACCESS_TOKEN`` env var when the file is
    missing — useful for CI hosts that bake the access token into a secret.
    Raises :class:`CredentialsNotFound` when neither source carries an
    ``access_token``.
    """
    env_token = os.environ.get("BSVIBE_ACCESS_TOKEN", "").strip()
    file_path = path or default_credentials_path()

    if file_path.exists():
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialsNotFound(f"failed to read credentials at {file_path}: {exc}") from exc
        access = payload.get("access_token")
        if isinstance(access, str) and access:
            return HostCredentials(
                access_token=access,
                refresh_token=payload.get("refresh_token") or None,
                expires_at=payload.get("expires_at"),
                issuer=payload.get("issuer") or None,
            )

    if env_token:
        return HostCredentials(
            access_token=env_token, refresh_token=None, expires_at=None, issuer=None
        )

    raise CredentialsNotFound(
        f"no credentials at {file_path} and BSVIBE_ACCESS_TOKEN is empty — "
        "run `bsvibe login` on this host first."
    )


def save_host_credentials(creds: HostCredentials, path: Path | None = None) -> Path:
    """Write ``creds`` to the credentials file (mode 0600). Returns the path."""
    file_path = path or default_credentials_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"access_token": creds.access_token}
    if creds.refresh_token:
        payload["refresh_token"] = creds.refresh_token
    if creds.expires_at is not None:
        payload["expires_at"] = creds.expires_at
    if creds.issuer:
        payload["issuer"] = creds.issuer
    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        file_path.chmod(0o600)
    except OSError:  # pragma: no cover — non-POSIX hosts
        logger.warning("credentials_chmod_failed", path=str(file_path))
    logger.info("host_credentials_saved", path=str(file_path))
    return file_path


def clear_host_credentials(path: Path | None = None) -> bool:
    """Delete the credentials file. Returns ``True`` if a file was removed."""
    file_path = path or default_credentials_path()
    if not file_path.exists():
        return False
    file_path.unlink()
    logger.info("host_credentials_cleared", path=str(file_path))
    return True


def save_worker_token(token: str, path: Path | None = None) -> Path:
    """Write ``token`` to the worker token file (mode 0600). Returns the path."""
    file_path = path or default_worker_token_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(token + "\n", encoding="utf-8")
    try:
        file_path.chmod(0o600)
    except OSError:  # pragma: no cover — non-POSIX hosts
        logger.warning("worker_token_chmod_failed", path=str(file_path))
    logger.info("worker_token_saved", path=str(file_path))
    return file_path


def load_worker_token(path: Path | None = None) -> str | None:
    """Return the saved worker token, or ``None`` when missing/empty."""
    file_path = path or default_worker_token_path()
    if not file_path.exists():
        return None
    try:
        raw = file_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def clear_worker_token(path: Path | None = None) -> bool:
    """Delete the worker token file. Returns ``True`` if a file was removed."""
    file_path = path or default_worker_token_path()
    if not file_path.exists():
        return False
    file_path.unlink()
    logger.info("worker_token_cleared", path=str(file_path))
    return True


# ── Worker config (Lift E12) ────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkerConfig:
    """Persisted register-time worker configuration (``~/.bsvibe/config.json``).

    Holds only what the founder explicitly chose at ``bsvibe-worker register``
    time — name, capabilities, labels, server URL, and a save timestamp. The
    bearer/host-OAuth credential is NOT stored here; it lives in
    ``~/.config/bsvibe/credentials.json``.
    """

    name: str
    capabilities: list[str]
    labels: list[str]
    server_url: str
    saved_at: int


def default_worker_config_path() -> Path:
    """Return ``$BSVIBE_HOME/config.json`` (or ``~/.bsvibe/config.json``)."""
    base = os.environ.get("BSVIBE_HOME")
    root = Path(base) if base else Path.home() / ".bsvibe"
    return root / _DEFAULT_WORKER_CONFIG_REL


def save_worker_config(config: WorkerConfig, path: Path | None = None) -> Path:
    """Write ``config`` to the worker config file (mode 0600). Returns the path."""
    file_path = path or default_worker_config_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "name": config.name,
        "capabilities": list(config.capabilities),
        "labels": list(config.labels),
        "server_url": config.server_url,
        "saved_at": config.saved_at,
    }
    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        file_path.chmod(0o600)
    except OSError:  # pragma: no cover — non-POSIX hosts
        logger.warning("worker_config_chmod_failed", path=str(file_path))
    logger.info("worker_config_saved", path=str(file_path))
    return file_path


def load_worker_config(path: Path | None = None) -> WorkerConfig | None:
    """Return the persisted ``WorkerConfig`` or ``None``.

    Returns ``None`` when the file is missing, corrupt, or has unexpected
    shape. Corrupt and malformed files emit a visible ``worker_config_corrupt``
    warning so the founder can see the file is in trouble — callers fall
    through to their defaults.
    """
    file_path = path or default_worker_config_path()
    if not file_path.exists():
        return None
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "worker_config_corrupt",
            path=str(file_path),
            hint="delete and re-register",
        )
        return None
    try:
        name = payload["name"]
        capabilities = payload["capabilities"]
        labels = payload["labels"]
        server_url = payload["server_url"]
        saved_at = payload["saved_at"]
    except (KeyError, TypeError):
        logger.warning(
            "worker_config_corrupt",
            path=str(file_path),
            hint="delete and re-register",
        )
        return None
    if not (
        isinstance(name, str)
        and isinstance(capabilities, list)
        and isinstance(labels, list)
        and isinstance(server_url, str)
        and isinstance(saved_at, int)
    ):
        logger.warning(
            "worker_config_corrupt",
            path=str(file_path),
            hint="delete and re-register",
        )
        return None
    return WorkerConfig(
        name=name,
        capabilities=[str(c) for c in capabilities],
        labels=[str(lab) for lab in labels],
        server_url=server_url,
        saved_at=saved_at,
    )


def clear_worker_config(path: Path | None = None) -> bool:
    """Delete the worker config file. Returns ``True`` if a file was removed."""
    file_path = path or default_worker_config_path()
    if not file_path.exists():
        return False
    file_path.unlink()
    logger.info("worker_config_cleared", path=str(file_path))
    return True


__all__ = [
    "CredentialsNotFound",
    "HostCredentials",
    "WorkerConfig",
    "clear_host_credentials",
    "clear_worker_config",
    "clear_worker_token",
    "default_credentials_path",
    "default_worker_config_path",
    "default_worker_token_path",
    "load_host_credentials",
    "load_worker_config",
    "load_worker_token",
    "save_host_credentials",
    "save_worker_config",
    "save_worker_token",
]
