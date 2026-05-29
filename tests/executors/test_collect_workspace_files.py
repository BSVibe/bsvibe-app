"""_collect_workspace_files — capture filters build/cache junk + binary.

Surfaced by a live prod dogfood: a design-stage codex run that executes its
tests leaves ``__pycache__/*.pyc`` + ``.pytest_cache/`` in the workspace. Those
got captured as artifacts and (being binary, with NUL bytes) crashed the impl
stage's Postgres write. The capture must skip such build/cache junk so only real
source artifacts are persisted.
"""

from __future__ import annotations

from pathlib import Path

from backend.executors.worker.main import _collect_workspace_files


def test_skips_pycache_pytest_cache_and_pyc(tmp_path: Path) -> None:
    (tmp_path / "rate_limiter.py").write_text("class TokenBucket: ...\n", encoding="utf-8")
    (tmp_path / "test_rate_limiter.py").write_text("def test_x(): ...\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "rate_limiter.cpython-311.pyc").write_bytes(b"\x00\x01\x00bin")
    (tmp_path / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (tmp_path / ".pytest_cache" / "CACHEDIR.TAG").write_text("Signature\n", encoding="utf-8")
    (tmp_path / ".pytest_cache" / "v" / "cache" / "nodeids").write_text("[]", encoding="utf-8")

    paths = {f["path"] for f in _collect_workspace_files(str(tmp_path))}

    assert paths == {"rate_limiter.py", "test_rate_limiter.py"}, paths
    assert not any(".pyc" in p or "__pycache__" in p or ".pytest_cache" in p for p in paths)
