"""Walker behaviour — pruning + caps + binary skip + symlink containment."""

from __future__ import annotations

import pytest

from backend.products.application.bootstrap.walker import (
    BootstrapTooLargeError,
    walk_repo,
)


def _make_file(root, rel: str, content: bytes = b"hi\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def test_walker_prunes_skip_dirs(tmp_path):
    _make_file(tmp_path, "src/app.py", b"print('hi')\n")
    _make_file(tmp_path, "node_modules/foo/index.js", b"x=1\n")
    _make_file(tmp_path, ".git/HEAD", b"ref\n")
    _make_file(tmp_path, "__pycache__/x.pyc", b"x\n")
    _make_file(tmp_path, "dist/bundle.js", b"x\n")

    rels = sorted(w.rel_path for w in walk_repo(tmp_path))
    assert rels == ["src/app.py"]


def test_walker_skips_binary_files(tmp_path):
    _make_file(tmp_path, "good.py", b"print('ok')\n")
    _make_file(tmp_path, "bad.bin", b"head\x00tail")

    rels = [w.rel_path for w in walk_repo(tmp_path)]
    assert "good.py" in rels
    assert "bad.bin" not in rels


def test_walker_skips_oversize_files(tmp_path):
    small = b"x" * 100
    big = b"x" * (600 * 1024)
    _make_file(tmp_path, "small.py", small)
    _make_file(tmp_path, "big.py", big)
    rels = [w.rel_path for w in walk_repo(tmp_path, max_file_bytes=500 * 1024)]
    assert "small.py" in rels
    assert "big.py" not in rels


def test_walker_raises_when_total_bytes_exceeded(tmp_path):
    # Two 300KB files — each under the per-file cap (500KB) but sum > 500KB total.
    payload = b"a" * (300 * 1024)
    _make_file(tmp_path, "a.py", payload)
    _make_file(tmp_path, "b.py", payload)
    with pytest.raises(BootstrapTooLargeError) as excinfo:
        list(walk_repo(tmp_path, max_total_bytes=500 * 1024))
    assert excinfo.value.metric == "bytes"


def test_walker_raises_when_file_count_exceeded(tmp_path):
    for i in range(5):
        _make_file(tmp_path, f"f{i}.py", b"x\n")
    with pytest.raises(BootstrapTooLargeError) as excinfo:
        list(walk_repo(tmp_path, max_file_count=3))
    assert excinfo.value.metric == "files"


def test_walker_skips_escaping_symlinks(tmp_path):
    outside = tmp_path.parent / "secret"
    outside.mkdir(exist_ok=True)
    secret = outside / "passwords.txt"
    secret.write_text("nope\n")
    _make_file(tmp_path, "real.py", b"x\n")
    link = tmp_path / "escape"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    rels = [w.rel_path for w in walk_repo(tmp_path)]
    assert "real.py" in rels
    assert "escape" not in rels


def test_walker_handles_empty_repo(tmp_path):
    assert list(walk_repo(tmp_path)) == []
