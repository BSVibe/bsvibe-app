"""§10 — the gate deriver had never been shown the repo's CI declarations.

prod 실측 (`bsvibe-prod-postgres-1`, 2026-08-25) — BSVibe 게이트 142건 중 명령을
낸 94건에서:

| 검사 | 포함률 |
|---|---|
| `ruff check` | 90 / 94 (96%) |
| `mypy` | 72 / 94 (77%) |
| `ruff format --check` | 42 / 94 (**45%**) |
| `lint-imports` | 15 / 94 (**16%**) |

CI 는 넷을 **전부** 요구한다. 갈리는 축은 딱 하나다: 앞의 둘은
`pyproject.toml` 에서 *추론 가능*하고(`ruff` 의존성 → `ruff check`), 뒤의 둘은
명령 이름이 `.github/workflows/ci.yml` 에**만** 있다. `_MANIFEST_FILES` 에 CI
파일이 하나도 없으니 deriver 는 그 이름을 본 적이 없다 — 그런데 시스템
프롬프트는 "CI 에 근거하라 / CI step 을 VERBATIM 으로 선호하라"고 지시한다.
따를 수 없는 지시였고, 45% / 16% 가 그 모양이다. PR #819 의 CI 를 떨어뜨린 것이
정확히 게이트가 빠뜨린 `ruff format --check` 였다.

⚠️ `_read_repo_manifests` 는 건드리지 않는다. 그 반환값은 두 곳에서 *fail-closed
판정*을 겸한다 (`_manifest_present()` · `inplace_gate` 의 `if not manifests`).
CI 파일을 섞으면 CI 만 있는 문서 레포가 "툴체인 있음"이 되어 **다른 질문**에
답하게 된다. 그래서 읽는 함수를 새로 만들고, 여기서 그 불변을 양성 대조군으로
고정한다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from backend.workflow.application.verification_service import (
    _CI_CTX_BYTES,
    _CI_MAX_FILES,
    _CI_TOTAL_CTX_BYTES,
    VerificationService,
)
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxResult

pytestmark = pytest.mark.asyncio


_CI_YML = (
    "name: CI\n"
    "jobs:\n"
    "  quality:\n"
    "    steps:\n"
    "      - run: uv run ruff check backend/\n"
    "      - run: uv run ruff format --check backend/\n"
    "      - run: uv run lint-imports\n"
)


class _Box:
    """A sandbox stand-in over a literal ``{path: text}`` tree.

    Deliberately NOT the tolerant ``FakeBox``: a real sandbox raises
    :class:`SandboxError` for a missing path, and "never a false-fail" is
    exactly the property under test here.
    """

    def __init__(self, files: dict[str, str], *, unreadable: set[str] | None = None) -> None:
        self.files = dict(files)
        self.unreadable = unreadable or set()
        self.read_calls: list[tuple[str, int]] = []
        self.list_calls: list[str] = []
        self.exec_calls: list[str] = []

    @property
    def workspace_mount(self) -> str:
        return "/workspace"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False, **_: Any):
        self.exec_calls.append(command)
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        self.read_calls.append((rel_path, max_bytes))
        if rel_path in self.unreadable:
            raise SandboxError(f"unreadable {rel_path}")
        if rel_path not in self.files:
            raise SandboxError(f"missing {rel_path}")
        return self.files[rel_path].encode()[:max_bytes]

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        raise AssertionError("the deriver never writes")

    async def list_dir(self, rel_path: str) -> list[str]:
        """``ls -A -p`` shape, like every real backend: directories carry a
        trailing ``/`` and a missing directory raises."""
        self.list_calls.append(rel_path)
        prefix = "" if rel_path in (".", "") else rel_path.rstrip("/") + "/"
        names: set[str] = set()
        for path in self.files:
            if not path.startswith(prefix):
                continue
            head, sep, _rest = path[len(prefix) :].partition("/")
            names.add(head + "/" if sep else head)
        if not names:
            raise SandboxError(f"missing dir {rel_path}")
        return sorted(names)


class _Llm:
    """Captures the messages the deriver was actually handed."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.messages: list[dict[str, str]] = []

    async def complete(self, *, messages: Any, tools: Any = None) -> Any:
        self.messages = list(messages)
        return type("_Turn", (), {"content": self._content})()

    @property
    def prompt(self) -> str:
        return "\n".join(str(m.get("content", "")) for m in self.messages)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


class _Run:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.payload: dict[str, Any] = {"intent_text": "add money utilities"}


_GATE_JSON = json.dumps({"applicable": True, "commands": [{"command": "true", "kind": "quality"}]})


def _service(llm: Any) -> VerificationService:
    return VerificationService(session=_FakeSession(), llm=llm, retriever=None)


def _real_worktree(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, run: _Run) -> None:
    """``_run_derived_gate`` only gates a run with a real server-side worktree."""
    import backend.storage.product_workspace as pw

    wt = tmp_path / str(run.id)
    (wt / ".git").mkdir(parents=True)
    monkeypatch.setattr(pw, "run_worktree_path", lambda _rid: wt)


# --------------------------------------------------------------------------
# The reader
# --------------------------------------------------------------------------


class TestReadCiDeclarations:
    async def test_reads_the_workflow_directory(self) -> None:
        box = _Box(
            {
                ".github/workflows/ci.yml": _CI_YML,
                ".github/workflows/release.yaml": "jobs: {publish: {}}\n",
                ".github/workflows/README.md": "not a workflow",
            }
        )
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        assert set(ci) == {".github/workflows/ci.yml", ".github/workflows/release.yaml"}
        assert "ruff format --check backend/" in ci[".github/workflows/ci.yml"]

    async def test_reads_well_known_single_file_declarations(self) -> None:
        box = _Box(
            {
                ".gitlab-ci.yml": "stages: [test]\n",
                ".circleci/config.yml": "version: 2.1\n",
                "Jenkinsfile": "pipeline { }\n",
                "azure-pipelines.yml": "trigger: [main]\n",
            }
        )
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        assert set(ci) == {
            ".gitlab-ci.yml",
            ".circleci/config.yml",
            "Jenkinsfile",
            "azure-pipelines.yml",
        }

    async def test_a_repo_with_no_ci_yields_an_empty_dict_not_an_error(self) -> None:
        """Every read raises on this box. A CI-less repo must be gateless-CI,
        never a false-fail — the deriver still runs on its manifests."""
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(_Box({}))
        assert ci == {}

    async def test_an_unreadable_file_is_skipped_not_fatal(self) -> None:
        box = _Box(
            {".github/workflows/ci.yml": _CI_YML, ".github/workflows/b.yml": "jobs: {}\n"},
            unreadable={".github/workflows/ci.yml"},
        )
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        assert set(ci) == {".github/workflows/b.yml"}

    async def test_a_sandbox_that_refuses_to_list_is_silently_empty(self) -> None:
        class _Hostile(_Box):
            async def list_dir(self, rel_path: str) -> list[str]:
                raise RuntimeError("worker went away")

        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(_Hostile({}))
        assert ci == {}

    async def test_the_file_count_is_bounded(self) -> None:
        box = _Box(
            {f".github/workflows/w{i:02d}.yml": f"jobs: {{j{i}: {{}}}}\n" for i in range(40)}
        )
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        assert 0 < len(ci) <= _CI_MAX_FILES

    async def test_the_total_byte_budget_is_bounded(self) -> None:
        box = _Box({f".github/workflows/w{i}.yml": "x" * 200_000 for i in range(5)})
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        assert sum(len(v) for v in ci.values()) <= _CI_TOTAL_CTX_BYTES

    async def test_the_total_budget_is_bytes_even_when_the_ci_is_not_ascii(self) -> None:
        """The budget is declared in BYTES (:data:`_CI_TOTAL_CTX_BYTES`) and a
        prompt window is spent in bytes, so a CI file written in a non-ASCII
        language must not buy more of it than an ASCII one.

        The ASCII case above cannot see this: there ``len(text)`` and the byte
        count are the same number, so a character-counted budget passes it
        forever. A Korean/Japanese/Chinese CI comment is 3 bytes per character —
        counting characters lets the CI block overrun by ~3x and crowd out the
        manifests, which is the exact failure the cap exists to prevent."""
        box = _Box({f".github/workflows/w{i}.yml": "가" * 200_000 for i in range(5)})
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        spent = sum(len(v.encode("utf-8")) for v in ci.values())
        assert spent <= _CI_TOTAL_CTX_BYTES, (
            f"CI grounding spent {spent}B of {_CI_TOTAL_CTX_BYTES}B"
        )

    async def test_each_non_ascii_file_stays_within_the_per_file_byte_cap(self) -> None:
        """Same confusion, one level down: the per-file cap is asked of the
        sandbox in bytes, so the text kept from it must be measured the same
        way."""
        box = _Box({".github/workflows/ci.yml": "가" * 200_000})
        ci = await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        assert all(len(v.encode("utf-8")) <= _CI_CTX_BYTES for v in ci.values())

    async def test_each_file_read_is_byte_capped_at_the_source(self) -> None:
        """The cap has to be asked of the SANDBOX, not applied after pulling a
        200MB file across the wire."""
        box = _Box({".github/workflows/ci.yml": _CI_YML})
        await _service(_Llm(_GATE_JSON))._read_ci_declarations(box)
        assert box.read_calls
        assert all(cap <= _CI_TOTAL_CTX_BYTES for _p, cap in box.read_calls)

    async def test_the_order_is_deterministic(self) -> None:
        """The prompt must be reproducible: the same repo derives the same
        grounding, so a gate that differs run to run is the model's doing and
        not ours."""
        files = {f".github/workflows/{n}.yml": f"jobs: {{{n}: {{}}}}\n" for n in "cab"}
        svc = _service(_Llm(_GATE_JSON))
        first = list(await svc._read_ci_declarations(_Box(files)))
        second = list(await svc._read_ci_declarations(_Box(files)))
        assert first == second == sorted(first)


# --------------------------------------------------------------------------
# POSITIVE CONTROL — the manifest reader's answer must not move
# --------------------------------------------------------------------------


class TestManifestReaderIsUnchanged:
    """`_read_repo_manifests` answers a DIFFERENT question, and two fail-closed
    decisions ride on it (`_manifest_present()` → "toolchain exists, so a
    deriver failure fails CLOSED"; `inplace_gate`'s `if not manifests: return
    None` → an honest gateless verdict). A docs repo that happens to run a CI
    workflow must keep reading as "no toolchain"."""

    async def test_a_repo_with_only_a_ci_file_still_has_zero_manifests(self) -> None:
        box = _Box({".github/workflows/ci.yml": _CI_YML, ".gitlab-ci.yml": "stages: [test]\n"})
        manifests = await _service(_Llm(_GATE_JSON))._read_repo_manifests(box)
        assert manifests == {}

    async def test_a_real_manifest_is_still_read(self) -> None:
        box = _Box(
            {"pyproject.toml": "[project]\nname = 'x'\n", ".github/workflows/ci.yml": _CI_YML}
        )
        manifests = await _service(_Llm(_GATE_JSON))._read_repo_manifests(box)
        assert set(manifests) == {"pyproject.toml"}


# --------------------------------------------------------------------------
# THE SEAM — CI text on disk must arrive in the derivation prompt
# --------------------------------------------------------------------------


class TestTheWireIsActuallyConnected:
    """Both ends green while the wire is cut is this repo's recurring defect.
    These read the CI step's VERBATIM text out of the messages the LLM was
    handed, through the production path — nothing is passed in by the test."""

    async def test_ci_text_reaches_the_deriver_through_the_server_side_path(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _Llm(_GATE_JSON)
        run = _Run()
        _real_worktree(monkeypatch, tmp_path, run)
        box = _Box(
            {"pyproject.toml": "[project]\nname = 'x'\n", ".github/workflows/ci.yml": _CI_YML}
        )
        await _service(llm)._run_derived_gate(run, box, ["money.py"])
        assert "ruff format --check backend/" in llm.prompt
        assert "lint-imports" in llm.prompt

    async def test_ci_text_reaches_the_deriver_through_the_in_place_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.workflow.application import inplace_gate as ig

        llm = _Llm(_GATE_JSON)
        run = _Run()
        box = _Box(
            {"pyproject.toml": "[project]\nname = 'x'\n", ".github/workflows/ci.yml": _CI_YML}
        )

        async def _changed(_box: Any, _baseline: Any) -> list[str]:
            return ["money.py"]

        monkeypatch.setattr(ig, "changed_paths", _changed)
        await ig.run_inplace_gate(_service(llm), run=run, box=box, baseline=None)
        assert "ruff format --check backend/" in llm.prompt
        assert "lint-imports" in llm.prompt
