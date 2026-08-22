"""감사 D6 — bootstrap 파일 분류 표가 세 모듈에 겹쳐 있다.

셋은 **파이프라인으로 합성**된다 (중복 호출이 아니다):

    walk_repo(repo_root, file_filter=BootstrapFileFilter(...)) → selector.classify(...)

문제는 호출이 아니라 **표**다. 실측(2026-08-22):

===============================  ================================  ==========================
표                                 어디에                             상태
===============================  ================================  ==========================
``_LOCKFILE_NAMES`` (9개)         ``bootstrap_filter`` + ``selector``  **바이트 동일**
디렉터리 스킵                       ``walker``(36) / ``filter``(32+2)   filter ⊂ walker — **우연**
``DEFAULT_MAX_FILE_BYTES``        ``walker``(500KB) / ``filter``(50KB)  **같은 이름 다른 값**
===============================  ================================  ==========================

## 왜 "우연"이 문제인가

``_handle_entry`` 는 **디렉터리를 먼저 잘라낸 뒤** 파일 단위로만 filter 를 부른다.
지금은 walker 의 스킵 목록이 filter 의 vendor/IDE 디렉터리를 **완전히 포함**해서
filter 의 그 분기들이 합성 경로에서 발화하지 않는다. 그런데 그건 **두 목록이 우연히
그런 관계라서**다 — 누가 filter 에만 vendor 디렉터리를 하나 추가하면 walker 는 그
디렉터리로 내려가고, 누가 walker 에만 추가하면 filter 목록이 낡는다.

**분기를 지우는 게 아니라 표를 하나로 만든다.** filter 를 단독으로 쓰는 코드가 생겨도
안전해야 하므로 방어 분기는 남긴다 — 고칠 것은 *두 개의 진실*이다.

## 건드리지 않는 것

* ``_IDE_CRUFT_FILES`` (``.DS_Store`` · ``Thumbs.db``) — walker 는 파일을 **이름으로 거르지
  않는다**. 이 분기는 **현역**이다.
* ``_BINARY_EXTENSIONS`` (47개) — walker 의 내용 스니핑이 대부분 잡지만 ``.svg`` / ``.pdf``
  는 텍스트로 보일 수 있어 통과한다. **부분 현역**이다.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PKG = _ROOT / "backend" / "products" / "application" / "bootstrap"


def _decl_sites(needle: str) -> list[str]:
    return [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _PKG.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith(needle)
    ]


def test_the_lockfile_table_is_declared_once() -> None:
    sites = _decl_sites("_LOCKFILE_NAMES: frozenset[str] = frozenset")
    assert len(sites) == 1, f"잠금파일 목록이 여러 곳에 선언돼 있다: {sites}"


def test_both_modules_share_the_same_lockfile_object() -> None:
    """특성화 — 미러가 아니라 **같은 객체**여야 한다."""
    from backend.products.application.bootstrap import bootstrap_filter as f
    from backend.products.application.bootstrap import selector as s

    assert f._LOCKFILE_NAMES is s._LOCKFILE_NAMES  # noqa: SLF001


def test_the_walk_skip_list_derives_from_the_filter_vocabulary() -> None:
    """**우연이던 포함 관계를 파생으로 바꾼다.**

    한쪽에만 vendor 디렉터리를 추가해도 다른 쪽이 낡지 않아야 한다."""
    from backend.products.application.bootstrap import bootstrap_filter as f
    from backend.products.application.bootstrap import walker as w

    assert f._VENDOR_DIRS <= w.DEFAULT_SKIP_DIRS  # noqa: SLF001
    assert f._IDE_CRUFT_DIRS <= w.DEFAULT_SKIP_DIRS  # noqa: SLF001
    # 파생이라는 증거 — walker 가 스스로 vendor 이름을 다시 적지 않는다.
    src = (_PKG / "walker.py").read_text(encoding="utf-8")
    assert '"node_modules"' not in src, "walker 가 vendor 이름을 다시 적었다"


def test_the_two_size_caps_no_longer_share_a_name() -> None:
    """같은 이름에 다른 값(500KB vs 50KB)이면 잘못된 import 하나로 조용히 바뀐다."""
    from backend.products.application.bootstrap import bootstrap_filter as f
    from backend.products.application.bootstrap import walker as w

    assert not hasattr(f, "DEFAULT_MAX_FILE_BYTES"), "filter 가 아직 같은 이름을 쓴다"
    assert w.DEFAULT_MAX_FILE_BYTES == 500 * 1024
    assert f.DEFAULT_MAX_PACKED_FILE_BYTES == 50 * 1024


def test_the_live_filter_branches_survive() -> None:
    """양성 대조군 — walker 가 **잡지 못하는** 것들. 지우면 크러프트가 새어 들어온다."""
    from backend.products.application.bootstrap import bootstrap_filter as f

    assert f._IDE_CRUFT_FILES == frozenset({".DS_Store", "Thumbs.db"})  # noqa: SLF001
    assert {".svg", ".pdf"} <= f._BINARY_EXTENSIONS  # noqa: SLF001


def test_the_pipeline_still_composes() -> None:
    """양성 대조군 — 셋은 합성된다. 그 배선이 그대로여야 한다."""
    import inspect

    from backend.products.application.bootstrap.walker import walk_repo

    assert "file_filter" in inspect.signature(walk_repo).parameters

    from backend.products.application.bootstrap.selector import FileBucket, classify

    assert classify("pyproject.toml") is not None
    assert len(list(FileBucket)) >= 3
