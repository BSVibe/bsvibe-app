"""비밀을 담는 ``.env`` 파일이 커밋 가능한 상태로 남지 않는다.

2026-09-12 실측: 배포 디렉터리에 ``.env.stale-local-dev`` 가 **untracked 이면서
gitignore 도 안 걸린 채** 넉 달(2026-05-29~) 있었다. 안에는 워커 토큰이 평문이었다
(죽은 워커 ``mac-mini-executor`` + ``localhost:8700`` 이라 prod 위협은 아니었고,
git 이력에도 한 번도 안 올라갔다 — 그래서 유출이 아니라 **노출 위험**이다).

원인은 패턴이 좁았던 것 하나다::

    .env
    .env.local
    .env.*.local        # ← 끝이 ``.local`` 이어야 매칭된다

``.env.stale-local-dev`` 는 ``.local`` 로 끝나지 않아 셋 다 비껴갔다. 이름에
``local`` 이 들어 있어서 **덮이는 것처럼 보이는 게 함정**이다.

이 테스트는 철자를 나열하지 않는다 — 그러면 상상 밖 철자가 또 통과한다
(스킬 ``absence-guard-listing-spellings-proves-only-imagination``). 대신 **저장소에
실재하는 ``.env`` 계열 파일 전부**를 훑어서, 각각이 *무시되거나* *example 이거나*
둘 중 하나임을 요구한다. 새 파일이 어떤 이름으로 생기든 이 집합에 들어온다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 커밋돼야 하는 것 — 값이 아니라 키 목록을 보여주는 템플릿.
_COMMITTABLE_SUFFIX = ".example"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def _env_files() -> list[Path]:
    """저장소 안의 ``.env`` 계열 파일 전부 (``.git`` / node_modules 제외)."""
    found: list[Path] = []
    for path in REPO_ROOT.rglob(".env*"):
        if not path.is_file():
            continue
        parts = path.relative_to(REPO_ROOT).parts
        if ".git" in parts or "node_modules" in parts or ".venv" in parts:
            continue
        found.append(path)
    return sorted(found)


def _is_ignored(path: Path, *, no_index: bool = False) -> bool:
    """``.gitignore`` 가 이 경로를 무시하는가.

    ``no_index=True`` 면 **추적 여부를 무시하고 패턴만** 본다. 이게 없으면
    이미 tracked 인 파일에 대해 git 이 항상 "무시 안 됨"을 보고하므로,
    패턴을 망가뜨려도 검사가 뒤집히지 않는다 — 실제로 이 테스트의 첫 판이
    그랬다(스킬 ``a-check-that-cannot-flip-is-not-measuring-anything``).
    """
    rel = path.relative_to(REPO_ROOT).as_posix()
    args = ["git", "check-ignore", "-q"]
    if no_index:
        args.append("--no-index")
    result = subprocess.run([*args, rel], cwd=REPO_ROOT, capture_output=True, check=False)
    return result.returncode == 0


def test_every_env_file_is_ignored_or_an_example() -> None:
    """``.env`` 계열은 무시되거나 ``.example`` 이거나 — 그 사이는 없다."""
    exposed = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in _env_files()
        if not p.name.endswith(_COMMITTABLE_SUFFIX) and not _is_ignored(p)
    ]
    assert not exposed, (
        "gitignore 에 안 걸리는 .env 파일이 있다 — `git add -A` 한 번이면 "
        f"비밀이 커밋된다: {exposed}. .gitignore 의 Env 절을 넓혀라."
    )


def test_the_example_templates_stay_addable() -> None:
    """음성 대조군 — 패턴을 넓히다가 template 까지 덮으면 이게 빨개진다.

    ⚠️ ``git ls-files`` 로 "추적되고 있나"를 물으면 **이 검사는 뒤집히지 않는다**:
    git 은 이미 tracked 인 파일에 무시 규칙을 적용하지 않으므로, ``!*.example``
    예외를 통째로 지워도 기존 template 은 추적 상태 그대로다. 첫 판이 실제로
    그랬고 전선을 끊어 보고서야 드러났다.

    그래서 ``--no-index`` 로 **패턴 자체**에 묻는다. 이러면 예외가 사라지는 순간
    빨개지고, "앞으로 새로 추가할 template 을 add 할 수 있는가"라는 진짜 명제를 잰다.
    """
    templates = [p for p in _env_files() if p.name.endswith(_COMMITTABLE_SUFFIX)]
    assert templates, "`.env*.example` 템플릿이 하나도 없다 — 대조군이 성립하지 않는다"

    covered = [
        p.relative_to(REPO_ROOT).as_posix() for p in templates if _is_ignored(p, no_index=True)
    ]
    assert not covered, (
        f".env 템플릿이 무시 패턴에 덮였다: {covered}. 지금 추적 중인 파일은 "
        "그대로 남지만 **새 template 은 `git add` 가 거부된다** — `!*.example` "
        "예외를 넣어라."
    )

    tracked = set(_git("ls-files").splitlines())
    missing = sorted({p.relative_to(REPO_ROOT).as_posix() for p in templates} - tracked)
    assert not missing, f".env 템플릿이 추적에서 빠졌다: {missing}"
