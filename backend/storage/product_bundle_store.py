"""ProductBundleStore — a product's durable home, off this box.

A product's canonical state is a git repo. Keeping that repo only on the Mac
Mini's disk makes it a SPOF (a disk failure loses every local product's source —
github-bound products are safe on GitHub) and lets the disk grow without bound
as products accumulate. This module is the seam that moves the repo's home to an
object store, so the disk holds only what is actively being worked on.

**The object is a git bundle, not a tarball.** ``git bundle create`` packs the
objects AND the refs, so ``git clone <bundle>`` restores a complete repo with
its history and branches. That is what keeps git's merge/conflict machinery
alive across a materialise → work → persist cycle; a working-tree tarball would
force last-write-wins and silently lose concurrent work.

Two implementations, one four-method seam (mirrors :class:`ArtifactStore`):

* :class:`LocalFilesystemBundleStore` — dev/test, and a legitimate production
  choice (a second disk / NAS mount).
* :class:`S3BundleStore` — Cloudflare R2 or any S3-compatible endpoint, over
  ``httpx`` + SigV4. No cloud SDK: the whole surface is PUT / GET / DELETE /
  HEAD of one object, and botocore's dependency tree dwarfs this project's.
  Verified against real R2 from inside the production container before it was
  written (PUT 200 / GET 200 byte-identical / DELETE 204 / GET 404).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import shutil
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx
import structlog

if TYPE_CHECKING:
    from backend.config import Settings

logger = structlog.get_logger(__name__)

#: Object-key namespace. Keeping products under one prefix lets an R2 lifecycle
#: rule or a bucket policy target them without touching e.g. DB backups sharing
#: the bucket.
_KEY_PREFIX = "products/"

_SIGV4_ALGORITHM = "AWS4-HMAC-SHA256"
#: R2 has no regions; the S3 API still requires a region in the credential
#: scope and expects the literal ``auto``.
_SIGV4_REGION = "auto"
_SIGV4_SERVICE = "s3"
#: sha256 of the empty byte string — the payload hash for a body-less request.
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@runtime_checkable
class ProductBundleStore(Protocol):
    """The durable-remote seam for a product's git bundle."""

    async def put(self, product_id: uuid.UUID, bundle_path: Path) -> None:
        """Upload ``bundle_path`` as the product's bundle, replacing any
        previous one. The newest successfully-pushed bundle IS the product."""

    async def get(self, product_id: uuid.UUID, dest_path: Path) -> bool:
        """Download the product's bundle to ``dest_path``.

        Returns ``False`` (leaving no file at ``dest_path``) when the product
        has no bundle yet — a product that has never shipped is an ordinary
        state, not an error, and the caller initialises an empty repo instead.
        """

    async def exists(self, product_id: uuid.UUID) -> bool:
        """``True`` iff the product has a bundle stored."""

    async def delete(self, product_id: uuid.UUID) -> None:
        """Remove the product's bundle. Idempotent — a missing object is a
        no-op, so the delete handler can retry freely."""


class LocalFilesystemBundleStore:
    """Bundles as files under ``<root>/products/<product_id>.bundle``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, product_id: uuid.UUID) -> Path:
        return self._root / f"{product_id}.bundle"

    async def put(self, product_id: uuid.UUID, bundle_path: Path) -> None:
        dest = self._path(product_id)

        def _copy() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Stage then rename so a crash mid-copy never leaves a truncated
            # bundle in place of a good one (a truncated bundle fails
            # ``git clone`` loudly, but the good bundle would already be gone).
            staging = dest.with_suffix(".bundle.partial")
            shutil.copyfile(bundle_path, staging)
            staging.replace(dest)

        # A bundle is a whole repo — tens of MB is normal. Copying it inline
        # would stall the event loop for every other run on this worker.
        await asyncio.to_thread(_copy)

    async def get(self, product_id: uuid.UUID, dest_path: Path) -> bool:
        src = self._path(product_id)

        def _copy() -> bool:
            if not src.is_file():
                dest_path.unlink(missing_ok=True)  # never leave a stale file
                return False
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest_path)
            return True

        return await asyncio.to_thread(_copy)

    async def exists(self, product_id: uuid.UUID) -> bool:
        return await asyncio.to_thread(self._path(product_id).is_file)

    async def delete(self, product_id: uuid.UUID) -> None:
        path = self._path(product_id)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


class S3BundleStore:
    """Bundles as S3 objects at ``<bucket>/products/<product_id>.bundle``.

    Signs every request with SigV4 over ``httpx``. Only the four operations the
    seam needs are implemented — there is deliberately no bucket creation or
    listing, because the production credential is bucket-scoped and cannot do
    either (the same constraint that forces ``--s3-no-check-bucket`` on the
    rclone backup path).
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        timeout_s: float = 180.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._timeout_s = timeout_s

    @staticmethod
    def _object_key(product_id: uuid.UUID) -> str:
        return f"{_KEY_PREFIX}{product_id}.bundle"

    def _sign(
        self,
        method: str,
        key: str,
        *,
        payload_sha256: str,
        amz_date: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Return ``(url, headers)`` for a SigV4-signed request.

        ``amz_date`` is injectable so a test can assert two signatures differ
        for a reason other than the clock.
        """
        url = f"{self._endpoint}/{self._bucket}/{urllib.parse.quote(key)}"
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        if amz_date is None:
            amz_date = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]

        canonical_headers = (
            f"host:{host}\nx-amz-content-sha256:{payload_sha256}\nx-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, parsed.path, "", canonical_headers, signed_headers, payload_sha256]
        )

        scope = f"{date_stamp}/{_SIGV4_REGION}/{_SIGV4_SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                _SIGV4_ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )

        def _hmac(key_bytes: bytes, msg: str) -> bytes:
            return hmac.new(key_bytes, msg.encode(), hashlib.sha256).digest()

        k_date = _hmac(f"AWS4{self._secret_key}".encode(), date_stamp)
        k_region = _hmac(k_date, _SIGV4_REGION)
        k_service = _hmac(k_region, _SIGV4_SERVICE)
        k_signing = _hmac(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

        headers = {
            "Authorization": (
                f"{_SIGV4_ALGORITHM} Credential={self._access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": amz_date,
            "host": host,
        }
        return url, headers

    async def _request(self, method: str, key: str, *, body: bytes | None = None) -> httpx.Response:
        payload = body or b""
        url, headers = self._sign(method, key, payload_sha256=hashlib.sha256(payload).hexdigest())
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            return await client.request(method, url, content=body, headers=headers)

    async def put(self, product_id: uuid.UUID, bundle_path: Path) -> None:
        # Reading a whole bundle is blocking disk I/O — keep it off the loop.
        body = await asyncio.to_thread(bundle_path.read_bytes)
        response = await self._request("PUT", self._object_key(product_id), body=body)
        if response.status_code >= 300:
            raise BundleStoreError(
                f"bundle upload failed for product {product_id}: HTTP {response.status_code}"
            )
        logger.info("product_bundle_pushed", product_id=str(product_id), bytes=len(body))

    async def get(self, product_id: uuid.UUID, dest_path: Path) -> bool:
        response = await self._request("GET", self._object_key(product_id))
        if response.status_code == 404:
            await asyncio.to_thread(lambda: dest_path.unlink(missing_ok=True))
            return False
        if response.status_code >= 300:
            raise BundleStoreError(
                f"bundle download failed for product {product_id}: HTTP {response.status_code}"
            )
        content = response.content

        def _write() -> None:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(content)

        await asyncio.to_thread(_write)
        return True

    async def exists(self, product_id: uuid.UUID) -> bool:
        response = await self._request("HEAD", self._object_key(product_id))
        if response.status_code == 404:
            return False
        if response.status_code >= 300:
            raise BundleStoreError(
                f"bundle probe failed for product {product_id}: HTTP {response.status_code}"
            )
        return True

    async def delete(self, product_id: uuid.UUID) -> None:
        response = await self._request("DELETE", self._object_key(product_id))
        # S3 DELETE is idempotent: a missing object still returns 204.
        if response.status_code >= 300 and response.status_code != 404:
            raise BundleStoreError(
                f"bundle delete failed for product {product_id}: HTTP {response.status_code}"
            )


class BundleStoreError(RuntimeError):
    """A bundle store operation failed. Carries no response body — an S3 error
    document can echo request metadata, and this message reaches logs."""


def build_bundle_store(settings: Settings | None = None) -> ProductBundleStore:
    """Construct the configured store.

    Misconfigured ``s3`` fails LOUD at construction rather than falling back to
    local disk: a silent fallback would leave products that the system reports
    as durable living on the one disk the whole design exists to get them off.
    """
    if settings is None:
        from backend.config import get_settings  # noqa: PLC0415

        settings = get_settings()

    backend = (getattr(settings, "product_bundle_backend", "local") or "local").lower()
    if backend == "local":
        return LocalFilesystemBundleStore(
            getattr(settings, "product_bundle_local_root", "var/bundles")
        )
    if backend == "s3":
        endpoint = getattr(settings, "product_bundle_s3_endpoint", "")
        bucket = getattr(settings, "product_bundle_s3_bucket", "")
        access_key = getattr(settings, "product_bundle_s3_access_key", "")
        secret_key = getattr(settings, "product_bundle_s3_secret_key", "")
        missing = [
            name
            for name, value in (
                ("product_bundle_s3_endpoint", endpoint),
                ("product_bundle_s3_bucket", bucket),
                ("product_bundle_s3_access_key", access_key),
                ("product_bundle_s3_secret_key", secret_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"product_bundle_backend='s3' but these settings are unset: {', '.join(missing)}"
            )
        return S3BundleStore(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
        )
    raise ValueError(f"unknown product_bundle_backend: {backend!r}")


__all__ = [
    "BundleStoreError",
    "LocalFilesystemBundleStore",
    "ProductBundleStore",
    "S3BundleStore",
    "build_bundle_store",
]
