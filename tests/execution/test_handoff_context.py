"""read_prior_step_context — fold the PRIOR step's output into a later step's
work context, read from product main or the prior run's dir.

The legacy ``design_*`` payload keys are still honoured (prod carries 11 such
runs from the deleted design→impl pipeline) — a deploy must not be able to
blind a run that was already mid-chain."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from backend.storage.artifact_store import LocalFilesystemArtifactStore
from backend.workflow.application.handoff import (
    capture_prior_step_output,
    read_prior_step_context,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        product_workspace_root=str(tmp_path / "products"),
        run_workspace_root=str(tmp_path / "runs"),
    )


def _impl_run(
    *, prior_run_id: uuid.UUID, refs: list[str], product_id: uuid.UUID | None
) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=product_id,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={
            "stage": "impl",
            "prior_run_id": str(prior_run_id),
            "prior_artifact_refs": refs,
        },
    )


def test_reads_the_prior_output_from_its_run_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prior_run_id = uuid.uuid4()
    LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
        prior_run_id, "docs/spec.md", b"# Spec\nBuild an adder.\n"
    )
    run = _impl_run(prior_run_id=prior_run_id, refs=["docs/spec.md"], product_id=None)

    out = read_prior_step_context(run, settings)  # type: ignore[arg-type]
    assert out is not None
    assert "docs/spec.md" in out
    assert "Build an adder." in out
    assert out.startswith("The prior step of this work produced")


def test_skips_binary_artifacts_so_no_nul_in_context(tmp_path: Path) -> None:
    """A captured binary artifact (e.g. a ``.pyc`` from the design stage running
    its tests) must NOT poison the impl prompt: NUL bytes are illegal in a
    Postgres text column and would crash the executor-task write. Binary refs are
    skipped; a text spec alongside still folds in, NUL-free."""
    settings = _settings(tmp_path)
    prior_run_id = uuid.uuid4()
    store = LocalFilesystemArtifactStore(Path(settings.run_workspace_root))
    store.put(prior_run_id, "rate_limiter.cpython-311.pyc", b"\x00\x01\x00binary\x00")
    store.put(prior_run_id, "rate_limiter.py", b"class TokenBucket:\n    ...\n")
    run = _impl_run(
        prior_run_id=prior_run_id,
        refs=["rate_limiter.cpython-311.pyc", "rate_limiter.py"],
        product_id=None,
    )

    out = read_prior_step_context(run, settings)  # type: ignore[arg-type]
    assert out is not None
    assert "\x00" not in out, "NUL byte leaked into the impl context (Postgres write would fail)"
    assert "class TokenBucket" in out  # the text spec still folds in
    assert ".pyc" not in out  # the binary artifact was skipped


def test_prefers_product_main_over_run_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prior_run_id = uuid.uuid4()
    product_id = uuid.uuid4()
    # Same ref present in BOTH locations with different content — product main wins.
    LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
        prior_run_id, "spec.md", b"stale run-dir copy"
    )
    LocalFilesystemArtifactStore(Path(settings.product_workspace_root)).put(
        product_id, "spec.md", b"shipped main copy"
    )
    run = _impl_run(prior_run_id=prior_run_id, refs=["spec.md"], product_id=product_id)

    out = read_prior_step_context(run, settings)  # type: ignore[arg-type]
    assert out is not None
    assert "shipped main copy" in out
    assert "stale run-dir copy" not in out


def test_none_for_an_unsplit_run(tmp_path: Path) -> None:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": "do a thing"},  # no prior_run_id / refs
    )
    assert read_prior_step_context(run, _settings(tmp_path)) is None  # type: ignore[arg-type]


def test_none_when_the_output_files_are_absent(tmp_path: Path) -> None:
    run = _impl_run(prior_run_id=uuid.uuid4(), refs=["missing.md"], product_id=None)
    assert read_prior_step_context(run, _settings(tmp_path)) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# D-2 — the output is captured INLINE at spawn (durable across worktree cleanup
# / a held run whose files never reached main). read_prior_step_context prefers
# the inlined text; capture_prior_step_output produces it.
# --------------------------------------------------------------------------


def test_read_prefers_the_inlined_text_over_disk(tmp_path: Path) -> None:
    """When the payload carries prior_output_text (inlined at spawn) it is used
    verbatim — no filesystem read (which may have been cleaned up)."""
    run = _impl_run(prior_run_id=uuid.uuid4(), refs=["docs/spec.md"], product_id=None)
    run.payload["prior_output_text"] = "The prior step produced:\n\n# Inlined spec"
    # No file exists on disk for the refs — proving the inline path is used.
    out = read_prior_step_context(run, _settings(tmp_path))  # type: ignore[arg-type]
    assert out == "The prior step produced:\n\n# Inlined spec"


def test_read_falls_back_to_disk_when_no_inline(tmp_path: Path) -> None:
    """A run seeded before inlining (no prior_output_text) still reads the refs
    from disk."""
    settings = _settings(tmp_path)
    prior_run_id = uuid.uuid4()
    LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
        prior_run_id, "spec.md", b"# disk spec"
    )
    run = _impl_run(prior_run_id=prior_run_id, refs=["spec.md"], product_id=None)
    out = read_prior_step_context(run, settings)  # type: ignore[arg-type]
    assert out is not None and "disk spec" in out


def test_capture_reads_the_output_from_the_prior_worktree(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prior_run_id = uuid.uuid4()
    LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
        prior_run_id, "docs/spec.md", b"# Spec\nBuild it.\n"
    )
    out = capture_prior_step_output(
        product_id=None,
        prior_run_id=prior_run_id,
        refs=["docs/spec.md"],
        settings=settings,  # type: ignore[arg-type]
    )
    assert out is not None
    assert "Build it." in out
    assert out.startswith("The prior step of this work produced")


def test_capture_returns_none_when_nothing_is_readable(tmp_path: Path) -> None:
    assert (
        capture_prior_step_output(
            product_id=None,
            prior_run_id=uuid.uuid4(),
            refs=["missing.md"],
            settings=_settings(tmp_path),  # type: ignore[arg-type]
        )
        is None
    )
    # No refs → None (nothing to capture).
    assert (
        capture_prior_step_output(
            product_id=None,
            prior_run_id=uuid.uuid4(),
            refs=[],
            settings=_settings(tmp_path),  # type: ignore[arg-type]
        )
        is None
    )


def test_legacy_design_payload_keys_are_still_read(tmp_path: Path) -> None:
    """back-compat — 삭제된 design→impl 파이프라인이 남긴 payload 로도 컨텍스트가
    닿는다. prod 에 11건 있고 전부 종료 상태지만, 배포가 살아있는 체인을 눈멀게
    할 수 있어서는 안 된다."""
    settings = _settings(tmp_path)
    legacy_run_id = uuid.uuid4()
    LocalFilesystemArtifactStore(Path(settings.run_workspace_root)).put(
        legacy_run_id, "spec.md", b"# legacy spec"
    )
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={
            "stage": "impl",
            "design_run_id": str(legacy_run_id),
            "design_artifact_refs": ["spec.md"],
        },
    )
    out = read_prior_step_context(run, settings)  # type: ignore[arg-type]
    assert out is not None and "legacy spec" in out


def test_legacy_inlined_spec_text_is_still_read(tmp_path: Path) -> None:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"stage": "impl", "design_spec_text": "# inlined legacy"},
    )
    assert read_prior_step_context(run, _settings(tmp_path)) == "# inlined legacy"  # type: ignore[arg-type]
