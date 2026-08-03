"""ProductBundleStore — the durable remote home for a product's git repo.

A product's repo is stored off-box as a **git bundle** (objects + refs, not a
working-tree tarball) so ``git clone <bundle>`` restores a complete repo with
its history and branches intact — which is what keeps git's merge/conflict
machinery alive across a materialise → work → persist cycle.

Two implementations, one seam:

* :class:`LocalFilesystemBundleStore` — dev/test, and a legitimate production
  choice (a second disk / NAS mount).
* :class:`S3BundleStore` — Cloudflare R2 (or any S3-compatible endpoint) over
  ``httpx`` + hand-rolled SigV4. Verified against real R2 from inside the
  production container before it was written: PUT 200 / GET 200 (byte-identical)
  / DELETE 204 / GET 404.
"""

from __future__ import annotations

import uuid

import pytest

from backend.storage.product_bundle_store import (
    LocalFilesystemBundleStore,
    ProductBundleStore,
    build_bundle_store,
)


@pytest.fixture
def store(tmp_path) -> LocalFilesystemBundleStore:
    return LocalFilesystemBundleStore(tmp_path / "bundles")


@pytest.mark.asyncio
async def test_local_store_satisfies_the_protocol(store) -> None:
    assert isinstance(store, ProductBundleStore)


@pytest.mark.asyncio
async def test_put_then_get_round_trips_bytes(store, tmp_path) -> None:
    """A bundle is opaque bytes to the store — it must come back identical or
    the restored repo is corrupt."""
    product_id = uuid.uuid4()
    src = tmp_path / "src.bundle"
    payload = b"PACK\x00\x01binary bundle bytes\xff\xfe" * 100
    src.write_bytes(payload)

    await store.put(product_id, src)

    dest = tmp_path / "fetched.bundle"
    assert await store.get(product_id, dest) is True
    assert dest.read_bytes() == payload


@pytest.mark.asyncio
async def test_get_returns_false_when_absent(store, tmp_path) -> None:
    """A product with no bundle yet (never shipped) is not an error — the
    caller falls back to initialising an empty repo."""
    dest = tmp_path / "nothing.bundle"
    assert await store.get(uuid.uuid4(), dest) is False
    assert not dest.exists()


@pytest.mark.asyncio
async def test_put_overwrites_previous_bundle(store, tmp_path) -> None:
    """Every persist replaces the product's single bundle object — the newest
    merge result IS the product."""
    product_id = uuid.uuid4()
    first, second = tmp_path / "a", tmp_path / "b"
    first.write_bytes(b"old")
    second.write_bytes(b"new")

    await store.put(product_id, first)
    await store.put(product_id, second)

    dest = tmp_path / "out"
    assert await store.get(product_id, dest) is True
    assert dest.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_exists_reflects_put_and_delete(store, tmp_path) -> None:
    product_id = uuid.uuid4()
    assert await store.exists(product_id) is False

    src = tmp_path / "x"
    src.write_bytes(b"data")
    await store.put(product_id, src)
    assert await store.exists(product_id) is True

    await store.delete(product_id)
    assert await store.exists(product_id) is False


@pytest.mark.asyncio
async def test_delete_is_idempotent(store) -> None:
    """Deleting a product that never shipped must not raise — the product
    delete handler is best-effort and may retry."""
    await store.delete(uuid.uuid4())
    await store.delete(uuid.uuid4())


@pytest.mark.asyncio
async def test_bundles_are_isolated_per_product(store, tmp_path) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    for pid, data in ((a, b"aaa"), (b, b"bbb")):
        src = tmp_path / f"{pid}.src"
        src.write_bytes(data)
        await store.put(pid, src)

    for pid, data in ((a, b"aaa"), (b, b"bbb")):
        dest = tmp_path / f"{pid}.out"
        assert await store.get(pid, dest) is True
        assert dest.read_bytes() == data


@pytest.mark.asyncio
async def test_get_does_not_leave_a_partial_file_on_miss(store, tmp_path) -> None:
    """A miss must not leave a truncated/empty file behind — the caller would
    hand it to ``git clone`` and get a confusing corruption error instead of a
    clean "no bundle yet"."""
    dest = tmp_path / "dest.bundle"
    dest.write_bytes(b"stale content from a previous fetch")

    assert await store.get(uuid.uuid4(), dest) is False
    assert not dest.exists(), "a miss must remove the stale destination"


# ---------------------------------------------------------------------------
# build_bundle_store — settings-driven selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_returns_local_store_by_default(tmp_path, monkeypatch) -> None:
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "product_bundle_backend", "local", raising=False)
    monkeypatch.setattr(
        settings, "product_bundle_local_root", str(tmp_path / "bundles"), raising=False
    )
    assert isinstance(build_bundle_store(settings), LocalFilesystemBundleStore)


@pytest.mark.asyncio
async def test_build_returns_s3_store_when_configured(tmp_path, monkeypatch) -> None:
    from backend.config import get_settings
    from backend.storage.product_bundle_store import S3BundleStore

    settings = get_settings()
    monkeypatch.setattr(settings, "product_bundle_backend", "s3", raising=False)
    monkeypatch.setattr(
        settings, "product_bundle_s3_endpoint", "https://acct.r2.example.com", raising=False
    )
    monkeypatch.setattr(settings, "product_bundle_s3_bucket", "bsvibe-products", raising=False)
    monkeypatch.setattr(settings, "product_bundle_s3_access_key", "AK", raising=False)
    monkeypatch.setattr(settings, "product_bundle_s3_secret_key", "SK", raising=False)
    assert isinstance(build_bundle_store(settings), S3BundleStore)


@pytest.mark.asyncio
async def test_build_rejects_s3_without_credentials(monkeypatch) -> None:
    """Fail LOUD at construction rather than silently falling back to local
    disk — a silent fallback would mean "durable" products that are not."""
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "product_bundle_backend", "s3", raising=False)
    monkeypatch.setattr(settings, "product_bundle_s3_endpoint", "", raising=False)
    monkeypatch.setattr(settings, "product_bundle_s3_bucket", "", raising=False)
    monkeypatch.setattr(settings, "product_bundle_s3_access_key", "", raising=False)
    monkeypatch.setattr(settings, "product_bundle_s3_secret_key", "", raising=False)
    with pytest.raises(ValueError, match="product_bundle"):
        build_bundle_store(settings)


# ---------------------------------------------------------------------------
# S3BundleStore — SigV4 signing (verified against real R2; here we assert the
# canonical request/signature shape without any network)
# ---------------------------------------------------------------------------


def test_s3_signing_produces_a_complete_sigv4_authorization() -> None:
    from backend.storage.product_bundle_store import S3BundleStore

    store = S3BundleStore(
        endpoint="https://acct.r2.cloudflarestorage.com",
        bucket="bsvibe-products",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    url, headers = store._sign(
        "PUT",
        "products/abc.bundle",
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb924" + "27ae41e4649b934ca495991b7852b855",
    )

    assert url == "https://acct.r2.cloudflarestorage.com/bsvibe-products/products/abc.bundle"
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")
    assert "/auto/s3/aws4_request" in auth, "R2 uses the 'auto' region"
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert "Signature=" in auth
    # The payload hash is signed AND sent — R2 rejects a mismatch.
    assert (
        headers["x-amz-content-sha256"]
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert headers["x-amz-date"].endswith("Z")


def test_s3_signature_changes_with_the_payload() -> None:
    """A signature that ignored the body would let a corrupted upload pass."""
    from backend.storage.product_bundle_store import S3BundleStore

    store = S3BundleStore(
        endpoint="https://acct.r2.example.com",
        bucket="b",
        access_key="AK",
        secret_key="SK",
    )
    _, h1 = store._sign("PUT", "k", payload_sha256="a" * 64, amz_date="20260803T000000Z")
    _, h2 = store._sign("PUT", "k", payload_sha256="b" * 64, amz_date="20260803T000000Z")
    assert h1["Authorization"] != h2["Authorization"]


def test_s3_object_key_is_namespaced_per_product() -> None:
    from backend.storage.product_bundle_store import S3BundleStore

    store = S3BundleStore(
        endpoint="https://acct.r2.example.com",
        bucket="b",
        access_key="AK",
        secret_key="SK",
    )
    product_id = uuid.UUID("2cad16bd-1258-4ab9-8f7d-74d403847354")
    assert store._object_key(product_id) == "products/2cad16bd-1258-4ab9-8f7d-74d403847354.bundle"
