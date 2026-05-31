"""P1-L2b: read_design_context — fold the prior design stage's spec into the
impl run's work context, read from product main or the design run's dir."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from backend.storage.artifact_store import LocalFilesystemArtifactStore
from backend.workflow.application.handoff import read_design_context
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        product_workspace_root=str(tmp_path / "products"),
        run_workspace_root=str(tmp_path / "runs"),
    )


def _impl_run(
    *, design_run_id: uuid.UUID, refs: list[str], product_id: uuid.UUID | None
) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=product_id,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={
            "stage": "impl",
            "design_run_id": str(design_run_id),
            "design_artifact_refs": refs,
        },
    )


def test_reads_design_spec_from_design_run_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    design_run_id = uuid.uuid4()
    LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
        design_run_id, "docs/spec.md", b"# Spec\nBuild an adder.\n"
    )
    run = _impl_run(design_run_id=design_run_id, refs=["docs/spec.md"], product_id=None)

    out = read_design_context(run, settings)  # type: ignore[arg-type]
    assert out is not None
    assert "docs/spec.md" in out
    assert "Build an adder." in out
    assert out.startswith("The prior DESIGN stage produced")


def test_skips_binary_artifacts_so_no_nul_in_context(tmp_path: Path) -> None:
    """A captured binary artifact (e.g. a ``.pyc`` from the design stage running
    its tests) must NOT poison the impl prompt: NUL bytes are illegal in a
    Postgres text column and would crash the executor-task write. Binary refs are
    skipped; a text spec alongside still folds in, NUL-free."""
    settings = _settings(tmp_path)
    design_run_id = uuid.uuid4()
    store = LocalFilesystemArtifactStore(Path(settings.run_workspace_root))
    store.put(design_run_id, "rate_limiter.cpython-311.pyc", b"\x00\x01\x00binary\x00")
    store.put(design_run_id, "rate_limiter.py", b"class TokenBucket:\n    ...\n")
    run = _impl_run(
        design_run_id=design_run_id,
        refs=["rate_limiter.cpython-311.pyc", "rate_limiter.py"],
        product_id=None,
    )

    out = read_design_context(run, settings)  # type: ignore[arg-type]
    assert out is not None
    assert "\x00" not in out, "NUL byte leaked into the impl context (Postgres write would fail)"
    assert "class TokenBucket" in out  # the text spec still folds in
    assert ".pyc" not in out  # the binary artifact was skipped


def test_prefers_product_main_over_run_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    design_run_id = uuid.uuid4()
    product_id = uuid.uuid4()
    # Same ref present in BOTH locations with different content — product main wins.
    LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
        design_run_id, "spec.md", b"stale run-dir copy"
    )
    LocalFilesystemArtifactStore(Path(settings.product_workspace_root)).put(
        product_id, "spec.md", b"shipped main copy"
    )
    run = _impl_run(design_run_id=design_run_id, refs=["spec.md"], product_id=product_id)

    out = read_design_context(run, settings)  # type: ignore[arg-type]
    assert out is not None
    assert "shipped main copy" in out
    assert "stale run-dir copy" not in out


def test_none_for_non_impl_run(tmp_path: Path) -> None:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": "do a thing"},  # no design_run_id / refs
    )
    assert read_design_context(run, _settings(tmp_path)) is None  # type: ignore[arg-type]


def test_none_when_spec_files_absent(tmp_path: Path) -> None:
    run = _impl_run(design_run_id=uuid.uuid4(), refs=["missing.md"], product_id=None)
    assert read_design_context(run, _settings(tmp_path)) is None  # type: ignore[arg-type]
