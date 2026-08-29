"""플러그인 seed 의 ``source_ref`` 를 지운다 — 아무도 읽지 않는 값이었다.

#846 이 커넥터 ``region`` 을 지운 것과 같은 모양이다. 다른 점은 하나뿐이다:
region 은 사용자가 채우는 **설정 필드**였고, 이것은 플러그인이 스스로 만들어
넣는 **출처 표시**다. 둘 다 수신자가 버린다.

## 실측이 보여준 것 (2026-08-29, prod)

* **프로덕션 독자 0** — 이 키를 읽는 코드는 플러그인 자기 자신 외에 없다.
  ``write_seed`` 는 ``data`` 에서 ``title`` · ``tags`` · ``content`` 만 읽는다
* **4개 플러그인 모두 title+content 를 채운다** → 본문이 ``data["content"]`` 로
  잡히는 분기를 타므로 ``source_ref`` 는 **노트 어디에도 도달하지 않는다**.
  (전량 YAML 덤프 폴백은 title 이나 content 가 **없을 때만** 탄다)
* **prod 설치 0** — 이 값을 만드는 커넥터 4종(claude · gpt · notion · obsidian)
  중 prod 에 설치된 것이 없고, ``connector_accounts`` 전 행의
  ``last_import_at`` 이 NULL 이다. 어느 워크스페이스에도 ``seeds/`` 디렉터리가
  없다 — ``write_seed`` 는 prod 에서 한 번도 실행된 적이 없다
  (양성 대조군: 같은 테이블에 github · telegram 6행이 살아 있다)

## 주석이 근거로 든 메커니즘은 이 값을 안 쓴다

네 플러그인이 똑같이 주장했다 — *"re-imports … hit the IngestCompiler
content-hash dedup on the same key"*. 실제 키는
``f"{rel_path}:{content_hash}"`` 로 **``source_ref`` 를 쓰지 않는다.**
게다가 ``rel_path`` 는 seed 파일마다 새 타임스탬프(``%Y-%m-%d_%H%M%S.md``)라
양쪽 절반이 다 달라지고, 그 캐시는 ``_MAX_CACHE_SIZE`` 로 축출되는 프로세스
메모리 LRU 라 애초에 영속 dedup 이 아니다.

∴ **재임포트 중복은 남는다.** 이 PR 은 그것을 고치지 않는다 — 지우는 것은
*막고 있다는 거짓 주장*이지 중복 방지 기능이 아니다. 인스턴스 0인 경로에
dedup 서브시스템을 짓지 않는다는 판단(형님, 2026-08-29)이고, 남는 갭은
``docs/`` 에 적힌다.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

#: 이름을 조립한다 — 일괄 rename/삭제 스크립트가 이 가드 자신을 오염시키지
#: 않도록. (#809 에서 needle 이 아니라 **조건 줄**이 통째로 지워져 가드가
#: 초록이 된 적이 있다.)
_KEY = "source" + "_ref"

#: ``write_seed`` 가 ``data`` 에서 실제로 읽는 키. 이 PR 의 삭제가 안전한
#: **이유** 그 자체이므로 명제 그대로 박아둔다.
_CONSUMED_SEED_KEYS = frozenset({"title", "tags", "content"})

_SCANNED = ("backend", "plugin", "bsvibe_sdk", "tests")


def _dict_string_keys(tree: ast.AST):
    """dict 리터럴 키 · 첨자 · ``.get()`` 에 쓰인 **문자열 상수**만 낸다.

    #845 의 가드는 식별자를 AST 로 셌다 — docstring·주석·산문이 후보가 아니게
    하려고. 여기서는 대상이 식별자가 아니라 **dict 문자열 키**라 그 축으로
    각색한다. 원리는 같다: 텍스트가 아니라 코드를 센다. 이 파일의 산문이
    ``source_ref`` 를 스무 번 말해도 후보가 되지 않는 이유다.
    """
    for node in ast.walk(tree):
        match node:
            case ast.Dict(keys=keys):
                for key in keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        yield key.value
            case ast.Subscript(slice=ast.Constant(value=str() as key)):
                yield key
            case ast.Call(
                func=ast.Attribute(attr="get"),
                args=[ast.Constant(value=str() as key), *_],
            ):
                yield key


def _keys_read_from(tree: ast.AST, variable: str):
    """``<variable>`` 에서 **읽어 가는** 문자열 키만 낸다.

    함수 안의 dict 키를 전부 세면 안 된다 — 첫 판에서 그렇게 썼다가
    ``write_seed`` 가 내보내는 이벤트 페이로드의 ``"path"`` 를 "data 에서 읽는
    키"로 잘못 셌다. 세는 대상은 ``data[k]`` · ``k in data`` · ``data.get(k)``
    세 형태뿐이다.
    """
    for node in ast.walk(tree):
        match node:
            case ast.Subscript(value=ast.Name(id=name), slice=ast.Constant(value=str() as key)) if (
                name == variable
            ):
                yield key
            case ast.Compare(
                left=ast.Constant(value=str() as key),
                ops=[ast.In() | ast.NotIn()],
                comparators=[ast.Name(id=name)],
            ) if name == variable:
                yield key
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id=name), attr="get"),
                args=[ast.Constant(value=str() as key), *_],
            ) if name == variable:
                yield key


def _scan(predicate) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name in _SCANNED:
        root = _ROOT / name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path == Path(__file__):
                continue
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            hits = {k for k in _dict_string_keys(module) if predicate(k)}
            if hits:
                found[str(path.relative_to(_ROOT))] = hits
    return found


def test_no_code_still_writes_or_reads_the_provenance_seed_key() -> None:
    """파일 집합이 아니라 **트리 전체**를 훑는다 — 새 플러그인도 자동으로 걸린다.

    철자 목록형 가드였다면 ``"source_ref"`` 만 막고 ``seed["source_ref"]`` 나
    ``data.get("source_ref")`` 를 놓쳤을 것이다. 세 형태를 다 세는 이유다.
    """
    offenders = _scan(lambda k: k == _KEY)
    assert not offenders, f"{_KEY} 를 아직 쓰는 코드가 남았다:\n" + "\n".join(
        f"  {f}: {sorted(ks)}" for f, ks in sorted(offenders.items())
    )


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_the_scanner_can_actually_find_a_seed_key() -> None:
    """**스캐너가 고장 나 있으면 위 부재 주장은 공짜로 참이 된다.**

    0을 세는 것은 생산자가 꺼져 있을 때 가장 쉽다. 살아남아야 하는 키
    (``content``)로 같은 스캐너를 먼저 돌려, 이 스캐너가 dict 문자열 키를
    실제로 찾아낼 수 있음을 증명한다.
    """
    found = _scan(lambda k: k == "content")
    plugins = {f for f in found if f.startswith("plugin/") and f.endswith("/plugin.py")}
    assert len(plugins) >= 4, f"스캐너가 플러그인 seed 키를 못 찾는다: {sorted(found)}"


def test_every_plugin_still_seeds_a_title_and_content() -> None:
    """양성 대조군 — 출처 표시를 떼면서 본문을 떼면 안 된다.

    이 둘이 **함께** 있어야 ``write_seed`` 가 본문을 ``data["content"]`` 로
    잡는다. 하나라도 빠지면 전량 YAML 덤프 폴백으로 떨어지고, 그러면 노트
    본문이 내부 dict 로 바뀐다.
    """
    for plugin in ("claude", "gpt", "notion", "obsidian"):
        path = _ROOT / "plugin" / plugin / "plugin.py"
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        keys = set(_dict_string_keys(module))
        assert {"title", "content"} <= keys, f"{plugin} 이 title/content 를 안 채운다: {keys}"


def test_write_seed_still_reads_only_title_tags_and_content() -> None:
    """삭제가 안전한 **이유**를 명제 그대로 박는다.

    누군가 나중에 ``write_seed`` 가 출처를 읽게 만들면 이 테스트가 떨어지고,
    그때는 이 PR 의 전제가 바뀐 것이다 — 가드를 고치기 전에 그 판단부터.
    """
    from backend.knowledge.graph.writer_core import _io

    source = Path(_io.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=_io.__file__)
    func = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "write_seed"
    )
    read = set(_keys_read_from(func, "data"))
    assert read == _CONSUMED_SEED_KEYS, f"write_seed 가 읽는 키가 바뀌었다: {sorted(read)}"


def test_the_ingest_dedup_key_still_does_not_involve_provenance() -> None:
    """거짓 주석이 근거로 든 그 메커니즘 — 실제로 무엇을 쓰는지 고정한다."""
    from backend.knowledge.ingest import llm_extractor

    source = Path(llm_extractor.__file__).read_text(encoding="utf-8")
    assert 'cache_key = f"{rel_path}:{content_hash}"' in source
    assert _KEY not in source
